from setuptools import setup

setup(
    name='subsearch',
    version_config={
        'version_format': '{tag}.dev{sha}',
        'starting_version': '0.0',
    },
    py_modules=['subsearch'],
    setup_requires=[
        'better-setuptools-git-version',
    ],
    install_requires=[
        'attrs>=18.0.0',
        'Click>=7.0',
        'pysubs2>=0.2.0',
        'requests>=2.0.0',
        'Whoosh>=2.0.0',
    ],
    entry_points='''
        [console_scripts]
        subsearch=subsearch:cli
    ''',
)
