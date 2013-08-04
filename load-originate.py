#!/usr/bin/env python
# vim: tabstop=4 softtabstop=4 shiftwidth=4 textwidth=80 smarttab expandtab
import sys
import os
import time
import sched
import ESL
import logging
from optparse import OptionParser

"""
TODO
- Implement -timeout (global timeout, we need to hupall all of our calls and exit)
- Implement -sleep to sleep x seconds at startup
- Implement fancy rate logic from sipp
- Implement sipp -trace-err option
- Implement sipp return codes (0 for success, 1 call failed, etc etc)
"""

class FastScheduler(sched.scheduler):

    def __init__(self, timefunc, delayfunc):
        self.queue = []
        # Do not use super() as sched.scheduler does not inherit from object
        sched.scheduler.__init__(self, timefunc, delayfunc)
        if sys.version_info[0] == 2 and sys.version_info[1] == 7:
            """
            Python 2.7 renamed the sched member queue to _queue, lets
            keep the old name in our class
            """
            self.queue = self._queue

    def next_event_time_delta(self):
        """
        Return the time delta in seconds for the next event
        to become ready for execution
        """
        q = self.queue
        if len(q) <= 0:
            return -1
        time, priority, action, argument = q[0]
        now = self.timefunc()
        if time > now:
            return int(time - now)
        return 0

    def fast_run(self):
        """
        Try to run events that are ready only and return immediately
        It is assumed that the callbacks will not block and the time
        is only retrieved once (when entering the function) and not
        before executing each event, so there is a chance an event
        that becomes ready while looping will not get executed
        """
        q = self.queue
        now = self.timefunc()
        while q:
            time, priority, action, argument = q[0]
            if now < time:
                break
            if now >= time:
                self.cancel(q[0])
                void = action(*argument)

class Session(object):
    def __init__(self, uuid):
        self.uuid = uuid
        self.answered = False

class SessionManager(object):
    def __init__(self, server, port, auth, logger,
            rate=1, limit=1, max_sessions=1, duration=60, originate_string=''):
        self.server = server
        self.port = port
        self.auth = auth
        self.rate = rate
        self.limit = limit
        self.max_sessions = max_sessions
        self.duration = duration
        self.originate_string = originate_string
        self.logger = logger

        self.sessions = {}
        self.hangup_causes = {}
        self.total_originated_sessions = 0
        self.total_answered_sessions = 0
        self.total_failed_sessions = 0
        self.terminate = False
        self.ev_handlers = {
            'CHANNEL_ORIGINATE': self.handle_originate,
            'CHANNEL_ANSWER': self.handle_answer,
            'CHANNEL_HANGUP': self.handle_hangup,
            'SERVER_DISCONNECTED': self.handle_disconnect,
        }

        self.sched = FastScheduler(time.time, time.sleep)
        # Initialize the ESL connection
        self.con = ESL.ESLconnection(self.server, self.port, self.auth)
        if not self.con.connected():
            logger.error('Failed to connect!')
            raise Exception

        # Raise the sps and max_sessions limit to make sure they do not obstruct our test
        self.con.api('fsctl sps %d' % (self.rate * 10))
        self.con.api('fsctl max_sessions %d' % (self.limit * 10))

        # Reduce logging level to avoid much output in console/logfile
        self.con.api('fsctl loglevel warning')

        # Register relevant events to get notified about our sessions created/destroyed
        self.con.events('plain', 'CHANNEL_ORIGINATE CHANNEL_ANSWER CHANNEL_HANGUP')

    def originate_sessions(self):
        self.logger.info('Originating sessions')
        if self.total_originated_sessions >= self.max_sessions:
            self.logger.info('Done originating')
            return
        sesscnt = len(self.sessions)
        for i in range(0, self.rate):
            if sesscnt >= self.limit:
                break
            self.con.api('bgapi originate %s' % (self.originate_string))
            sesscnt = sesscnt + 1
        self.sched.enter(1, 1, self.originate_sessions, [])

    def process_event(self, e):
        evname = e.getHeader('Event-Name')
        if evname in self.ev_handlers:
            try:
                self.ev_handlers[evname](e)
                # When a new session is created that belongs to us, we can 
                # call sched_hangup to hangup the session at x interval
            except Exception, ex:
                self.logger.error('Failed to process event %s: %s' % (e, ex))
        else:
            self.logger.error('Unknown event %s' % (e))

    def handle_originate(self, e):
        uuid = e.getHeader('Channel-Call-UUID')
        dir = e.getHeader('Call-Direction')
        if dir != 'outbound':
            # Ignore non-outbound calls (allows looping calls back on the DUT)
            return
        self.logger.debug('Originated session %s' % uuid)
        if uuid in self.sessions:
            self.logger.error('WTF? duplicated originate session %s' % (uuid))
            return
        self.sessions[uuid] = Session(uuid)
        self.total_originated_sessions = self.total_originated_sessions + 1
        self.con.api('sched_hangup +%d %s NORMAL_CLEARING' % (self.duration, uuid))

    def handle_answer(self, e):
        uuid = e.getHeader('Channel-Call-UUID')
        if uuid not in self.sessions:
            return
        self.logger.debug('Answered session %s' % uuid)
        self.total_answered_sessions = self.total_answered_sessions + 1
        self.sessions[uuid].answered = True

    def handle_hangup(self, e):
        uuid = e.getHeader('Channel-Call-UUID')
        if uuid not in self.sessions:
            return
        cause = e.getHeader('Hangup-Cause')
        if cause not in self.hangup_causes:
            self.hangup_causes[cause] = 1
        else:
            self.hangup_causes[cause] = self.hangup_causes[cause] + 1
        if not self.sessions[uuid].answered:
            self.total_failed_sessions = self.total_failed_sessions + 1
        del self.sessions[uuid]
        self.logger.debug('Hung up session %s' % uuid)
        if (self.total_originated_sessions >= self.max_sessions \
            and len(self.sessions) == 0):
            self.terminate = True

    def handle_disconnect(self):
        self.logger.error('Disconnected from server!')
        self.terminate = True

    def run(self):
        self.originate_sessions()
        while True:
            self.sched.fast_run()
            sched_sleep = self.sched.next_event_time_delta()
            if sched_sleep == 0:
                sched_sleep = 1
            e = self.con.recvEventTimed((sched_sleep * 1000))
            if e is None:
                continue
            self.process_event(e)
            if self.terminate:
                break

def main(argv):

    formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
    logger = logging.getLogger(os.path.basename(sys.argv[0]))
    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Try to emulate sipp options (-r, -l, -d, -m)
    parser = OptionParser()
    parser.add_option("-a", "--auth", dest="auth", default="ClueCon",
                    help="ESL password")
    parser.add_option("-s", "--server", dest="server", default="127.0.0.1",
                    help="FreeSWITCH server IP address")
    parser.add_option("-p", "--port", dest="port", default="8021",
                    help="FreeSWITCH server event socket port")
    parser.add_option("-r", "--rate", dest="rate", default=1,
                    help="Rate in sessions to run per second")
    parser.add_option("-l", "--limit", dest="limit", default=1,
                    help="Limit max number of concurrent sessions")
    parser.add_option("-d", "--duration", dest="duration", default=60,
                    help="Max duration in seconds of sessions before being hung up")
    parser.add_option("-m", "--max-sessions", dest="max_sessions", default=1,
                    help="Max number of sessions to originate before stopping")
    parser.add_option("-o", "--originate-string", dest="originate_string",
                    help="FreeSWITCH originate string")

    (options, args) = parser.parse_args()

    if not options.originate_string:
        print '-o is mandatory'
        sys.exit(1)

    sm = SessionManager(options.server, options.port, options.auth, logger,
            rate=int(options.rate), limit=int(options.limit), duration=int(options.duration),
            max_sessions=int(options.max_sessions), originate_string=options.originate_string)

    try:
        sm.run()
    except KeyboardInterrupt:
        pass

    print 'Total originated sessions: %d' % sm.total_originated_sessions
    print 'Total answered sessions: %d' % sm.total_answered_sessions
    print 'Total failed sessions: %d' % sm.total_failed_sessions
    print '-- Call Hangup Stats --'
    for cause, count in sm.hangup_causes.iteritems():
        print '%s: %d' % (cause, count)
    print '-----------------------'

    sys.exit(0)

if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception, e:
        print "Exception caught: %s" % (e)
