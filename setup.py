try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

setup(name='devito',
      version='2.0.1',
      description="""Finite Difference DSL for symbolic stencil computation.""",
      author="Imperial College London",
      license='MIT',
      packages=['devito'])
