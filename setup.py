from distutils.core import setup
from passerd import version
setup(name='passerd',
      version=version.VERSION,
      description='Passerd IRC-Twitter gateway',
      url=version.URL,
      author='Eduardo Habkost',
      author_email='ehabkost@raisama.net',
      license='MIT',
      platforms='any',
      packages=['passerd'],
      scripts=['bin/passerd'],
    )
