from passerd.feeds import ErrorThrottler
import unittest

class TestErrorThrottle(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.t = ErrorThrottler(lambda m: self.log.append(m))

    def assertLog(self, *l):
        self.assertEquals(self.log, list(l))

    def testSimpleMsg(self):
        self.t.error("ouch")
        self.assertLog("ouch")

    def testErrorOne(self):
        self.t.MAX_SAME_ERROR = 1
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.assertLog("d'oh", self.t.SAME_ERROR_MSG)

    def testErrorMany(self):
        self.t.MAX_SAME_ERROR = 2
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.assertLog("d'oh", "d'oh", self.t.SAME_ERROR_MSG)

    def testErrorBackError(self):
        self.t.MAX_SAME_ERROR = 2
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.ok()
        self.t.error("ouch")
        self.t.ok()
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.ok()
        self.t.ok()
        self.t.error("ouch")
        self.t.ok()
        self.t.ok()
        self.t.error("ouch")
        self.t.ok()
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.ok()
        self.t.error("ouch")
        self.t.ok()

        self.assertLog('ouch', 'ouch',
                       self.t.SAME_ERROR_MSG,
                       self.t.BACK_WORKING,
                       'ouch',
                       'ouch', 'ouch',
                       self.t.SAME_ERROR_MSG,
                       self.t.BACK_WORKING,
                       'ouch',
                       'ouch',
                       'ouch', 'ouch',
                       'ouch')

    def testFewDiffErrors(self):
        self.t.MAX_SAME_ERROR = 2
        self.t.MAX_DIFF_ERROR = 6
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("d'oh")
        self.t.error("ouch")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.ok()
        self.t.error("argh")

        self.assertLog("ouch",
                       "ouch",
                       "d'oh",
                       "ouch",
                       "d'oh",
                       "d'oh",
                       "argh")

    def testManyDifferrors(self):
        self.t.MAX_SAME_ERROR = 2
        self.t.MAX_DIFF_ERROR = 6
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("d'oh")
        self.t.error("ouch")
        self.t.error("d'oh")
        self.t.error("d'oh")
        self.t.error("argh")
        for i in range(100):
            self.t.error("error %d" % (i))
        self.t.ok()
        self.t.error("another error")
        self.t.error("another error")
        self.t.error("another error")
        self.t.error("another error")
        self.t.error("another error")
        self.t.error("another error")
        self.t.ok()
        self.t.ok()
        self.t.error("ouch")
        self.t.error("ouch")
        self.t.error("d'oh")
        self.t.error("ouch")
        self.t.error("d'oh")
        self.t.error("d'oh")

        self.assertLog("ouch",
                       "ouch",
                       "d'oh",
                       "ouch",
                       "d'oh",
                       "d'oh",
                       self.t.LOTS_ERRORS_MSG,
                       self.t.BACK_WORKING,
                       "another error", "another error",
                       self.t.SAME_ERROR_MSG,
                       self.t.BACK_WORKING,
                       "ouch",
                       "ouch",
                       "d'oh",
                       "ouch",
                       "d'oh",
                       "d'oh")

    def testNonStringError(self):
        self.t.MAX_SAME_ERROR = 2
        self.t.error("ouch")
        self.t.error("123")
        self.t.error(123)
        self.t.error(u"123")

        self.assertLog("ouch", '123', 123, self.t.SAME_ERROR_MSG)
