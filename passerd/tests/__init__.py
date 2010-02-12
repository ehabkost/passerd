import unittest, doctest

modules = 'dialogs formatting encoding errors'.split()
docmodules = []

def suite():
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    for n in modules:
        passerd = __import__('passerd.tests.%s' % (n))
        module = getattr(passerd.tests, n)
        tests = loader.loadTestsFromModule(module)
        suite.addTests(tests)

    for dm in docmodules:
        suite.addTest(doctest.DocTestSuite(dm))

    return suite

if __name__ == '__main__':
    unittest.TextTestRunner().run(suite())
