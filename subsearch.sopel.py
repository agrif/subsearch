#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import print_function

import tempfile
import subprocess
import os
import os.path
import logging
import re
import multiprocessing
import functools

import sopel.module
import sopel.tools
import sopel.logger
from sopel.config.types import StaticSection, ValidatedAttribute, ChoiceAttribute, ListAttribute

CONFIG_NAME = 'sonar'

log = sopel.logger.get_logger(CONFIG_NAME)
log.setLevel(logging.DEBUG)

def multiprocessify(func):
    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        return multiprocessing.Process(target=func, args=pargs, kwargs=kwargs)
    return wrapper

def getWorkerLogger(worker_name, level=logging.DEBUG):
    logging.basicConfig()
    log = logging.getLogger('sopel.modules.{}.{}-{:05d}'.format(CONFIG_NAME, worker_name, os.getpid()))
    log.setLevel(level)
    return log

class SonarSection(StaticSection):
    python_path     = ValidatedAttribute('python_path', str)
    script_path     = ValidatedAttribute('script_path', str)
    db_path         = ValidatedAttribute('db_path', str)

def setup(bot):
    bot.config.define_section(CONFIG_NAME, SonarSection)

    # if not bot.memory.contains(CONFIG_NAME):
    #     bot.memory[CONFIG_NAME] = sopel.tools.SopelMemory()

@sopel.module.commands('animeme')
def cmd_animeme(bot, trigger):
    words = trigger.group(2).strip().lower().split()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            proc = subprocess.run([
                    bot.config.sonar.python_path,
                    bot.config.sonar.script_path,
                    'search',
                    bot.config.sonar.db_path,
                    '-Ru'] + words,
                cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.stdout.decode('utf-8')
        if out.strip():
            m = re.search(r'Url: (.*)', out)
            if m:
                url = m.group(1)
                bot.reply(url)
            else:
                log.debug(out)
                bot.reply('Something went wrong, no url found')
        else:
            bot.reply('Meme harder scrub')
    except Exception as err:
        log.exception(err)
        bot.reply('Something went very wrong: %s' % err) 
cmd_animeme.priority = 'medium'
