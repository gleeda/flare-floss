#!/usr/bin/env python
# -*- coding: utf-8 -*-


try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup


requirements = [
    "q",
    "pyyaml",
    "tabulate",
    "vivisect",
    "plugnplay",
    "viv-utils",
    "enum34"
]

setup(
    name='floss',
    version='1.1.0',
    description="",
    long_description="",
    author="Willi Ballenthin, Moritz Raabe",
    author_email='william.ballenthin@mandiant.com, moritz.raabe@mandiant.com',
    url='https://www.github.com/fireeye/flare-floss',
    packages=[
        'floss',
    ],
    package_dir={'floss': 'floss'},
    package_data={'floss': ['plugins/*.py']},
    entry_points={
        "console_scripts": [
            "floss=floss.main:main",
        ]
    },
    include_package_data=True,
    install_requires=requirements,
    zip_safe=False,
    keywords='floss',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Natural Language :: English',
        "Programming Language :: Python :: 2",
    ],
)
