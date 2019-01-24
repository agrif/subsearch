#!/usr/bin/env python3
import subprocess
import json
import os
import os.path
import random
import tempfile

import pysubs2
import attr
import whoosh.fields
import whoosh.index
import whoosh.qparser
import click
import requests

@attr.s
class NonErrorFilter:
    name = attr.ib()

    def filter(self, record):
        return record.levelno < logging.WARNING

@attr.s
class FFmpeg:
    cmd = attr.ib()

    def run(self, *args):
        args_actual = [
            self.cmd, '-nostdin', '-y', '-hide_banner',
            '-loglevel', 'panic', '-nostats'] + list(args)
        # click.echo('ffmpeg-cmd=' + ' '.join(args_actual))
        return subprocess.check_output(args_actual)

    def read_subs(self, path):
        out = self.run('-i', path, '-f', 'ass', '-')
        return pysubs2.SSAFile.from_string(out.decode('utf-8'))

    def get_image(self, path, start, time, name):
        try:
            self.run(
                '-ss', str(start / 1000),
                '-i', path,
                '-copyts',
                '-ss', str(time / 1000),
                '-filter_complex', "subtitles='{}'".format(
                    path.replace("'", r"\'").replace(':', r'\:')),
                '-vframes', '1',
                '-f', 'image2',
                name)
        except subprocess.CalledProcessError as err:
            self.run(
                '-ss', str(start / 1000),
                '-i', path,
                '-copyts',
                '-ss', str(time / 1000),
                '-filter_complex', '[0:v][0:s]overlay[v]',
                '-map', '[v]',
                '-vframes', '1',
                '-f', 'image2',
                name)

@attr.s
class Result:
    path = attr.ib()
    content = attr.ib()
    start = attr.ib()
    end = attr.ib()

    @property
    def midpoint(self):
        return (self.start + self.end) / 2

@attr.s
class Database:
    path = attr.ib()
    ix = attr.ib()
    relative = attr.ib()
    
    @classmethod
    def create(cls, path, relative=False):
        schema = whoosh.fields.Schema(
            path=whoosh.fields.ID(stored=True),
            start=whoosh.fields.NUMERIC(stored=True),
            end=whoosh.fields.NUMERIC(stored=True),
            content=whoosh.fields.TEXT(stored=True),
        )
        config = dict(
            relative=relative,
        )
        os.makedirs(path, exist_ok=True)
        ix = whoosh.index.create_in(path, schema)
        with open(os.path.join(path, 'subsearch-config.json'), 'w') as f:
            json.dump(config, f)
        return cls(path, ix, relative)

    @classmethod
    def open(cls, path):
        ix = whoosh.index.open_dir(path)
        with open(os.path.join(path, 'subsearch-config.json')) as f:
            config = json.load(f)
        return cls(path, ix, **config)

    def search(self, query, **kwargs):
        q = whoosh.qparser.QueryParser("content", self.ix.schema).parse(query)
        with self.ix.searcher() as searcher:
            for r in searcher.search(q, **kwargs):
                d = dict(r)
                d['path'] = os.path.normpath(os.path.join(self.path, d['path']))
                yield Result(**d)

    def add_recursive(self, ff, path, **kwargs):
        for d in sorted(os.listdir(path)):
            full = os.path.join(path, d)
            self.add(ff, full, **kwargs)

    def add(self, ff, path, report=None, relative=None):
        if relative is None:
            relative = self.relative
        if os.path.isdir(path):
            return self.add_recursive(ff, path, report=report, relative=relative)

        realpath = path
        if relative:
            path = os.path.normpath(os.path.relpath(path, self.path))
        else:
            path = os.path.abspath(path)
        if report:
            report(path)

        try:
            subs = ff.read_subs(realpath)
        except subprocess.CalledProcessError:
            if report:
                report("!!! Error extracting subtitles...")
            return

        writer = self.ix.writer()
        for ev in subs.events:
            if ev.is_comment:
                continue
            writer.add_document(path=path, start=ev.start, end=ev.end, content=ev.plaintext)
        writer.commit()

@click.group()
def cli():
    pass

@cli.command()
@click.option('--relative/--absolute', '-r/-a', is_flag=True)
@click.argument('dbpath', type=click.Path())
def init(dbpath, relative):
    Database.create(dbpath, relative=relative)

@cli.command()
@click.option('--relative/--absolute', '-r/-a', is_flag=True, default=None)
@click.argument('dbpath', type=click.Path())
@click.argument('paths', nargs=-1, type=click.Path(exists=True))
def add(dbpath, paths, relative):
    db = Database.open(dbpath)
    ff = FFmpeg('ffmpeg')
    def report(s):
        click.echo('adding: {}'.format(s))
    for path in paths:
        db.add(ff, path, report=report, relative=relative)

@cli.command()
@click.option('--image', '-i', type=click.Path())
@click.option('--rand', '-R', is_flag=True)
@click.option('--upload', '-u', is_flag=True)
@click.argument('dbpath', type=click.Path())
@click.argument('query', nargs=-1)
def search(dbpath, query, upload=False, image=None, rand=False):
    if isinstance(query, (list, tuple)):
        query = ' '.join(query)
    if image is None:
        image = query.strip().replace(' ', '+') + '.png'
    image_fn = image
    db = Database.open(dbpath)
    ff = FFmpeg('ffmpeg')
    r = list(db.search(query))

    def do_upload(imgpath):
        with open(imgpath, 'rb') as imghandle:
            resp = requests.post('https://0x0.st/', files={'file': imghandle})
        click.echo('Url: {}'.format(resp.text.strip()))

    if not r:
        return

    res = [random.choice(r),] if rand else r

    for i, ev in enumerate(res):
        click.echo('Path: {}'.format(ev.path))
        click.echo('Time: {:.03f} - {:.03f}'.format(ev.start / 1000, ev.end / 1000))
        click.echo('Content: {}'.format(' \\ '.join(ev.content.strip().splitlines())))

        if not rand:
            base, ext = os.path.splitext(image)
            image_fn = '%s%03d%s' % (base, i, ext)

        ff.get_image(ev.path, ev.start, ev.midpoint, image_fn)
        click.echo('Image: {}'.format(image_fn))

        if upload:
            do_upload(image_fn)

if __name__ == "__main__":
    cli()
