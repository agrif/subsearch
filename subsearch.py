#!/usr/bin/env python3
import subprocess
import json
import os
import os.path
import random
import tempfile
import re

import pysubs2
import attr
import whoosh.fields
import whoosh.index
import whoosh.qparser
import click
import requests

@attr.s
class FFmpeg:
    cmd = attr.ib()

    def run(self, *args, **kwargs):
        args_actual = [
            self.cmd, '-nostdin', '-y', '-hide_banner',
            '-loglevel', 'panic', '-nostats'] + list(args)
        # click.echo('# ' + ' '.join(args_actual))
        return subprocess.run(args_actual, check=True, stdout=subprocess.PIPE, **kwargs)

    def read_subs(self, path):
        out = self.run('-i', path, '-f', 'ass', '-').stdout
        return pysubs2.SSAFile.from_string(out.decode('utf-8'))

    def read_silences(self, path, noise=-20):
        # need loglevel=info here because silencedetect outputs via log and
        # loglevel=panic is set by run()
        out = self.run('-loglevel', 'info',
            '-i', path,
            '-af', 'silencedetect=noise=%ddB:d=0.4' % noise,
            '-f', 'null', '-', stderr=subprocess.PIPE).stderr.decode('utf-8')

        silence_starts = re.findall(r'silence_start:\s+([\de.+-]+)', out)
        silence_ends = re.findall(r'silence_end:\s+([\d.e+-]+)', out)
        silence_durations = re.findall(r'silence_end.*silence_duration:\s+([\d.e+-]+)', out)

        if len(silence_starts) != len(silence_ends) or len(silence_starts) != len(silence_durations):
            raise ValueError('Non-matching length of silence detections: %d/%d/%d' % 
                (silence_starts, silence_ends, silence_durations))

        return sorted(zip(map(float, silence_starts), map(float, silence_ends), map(float, silence_durations)), key=lambda s: s[0])

    def get_clip(self, path, start, time, name):
        try:
            for p in range(1, 3):
                self.run(
                    '-y',
                    '-ss', str(start),
                    '-i', path,
                    '-t', str(time),
                    '-filter_complex', "subtitles='{}'".format(
                        path.replace("'", r"\'").replace(':', r'\:')),
                    '-c:v', 'libvpx-vp9',
                    '-crf', '15',
                    '-b:v', '0',
                    '-c:a', 'libopus',
                    '-b:a', '128k',
                    '-pass', str(p),
                    '-f', 'webm',
                    name)
        except subprocess.CalledProcessError as err:
            for p in range(1, 3):
                self.run(
                    '-y',
                    '-ss', str(start),
                    '-i', path,
                    '-t', str(time),
                    '-filter_complex', '[0:v][0:s]overlay[v]',
                    '-map', '[v]',
                    '-c:v', 'libvpx-vp9',
                    '-crf', '15',
                    '-b:v', '0',
                    '-c:a', 'libopus',
                    '-b:a', '128k',
                    '-pass', str(p),
                    '-f', 'webm',
                    name)

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
@click.option('--webm', '-w', is_flag=True)
@click.option('--noise', '-n', default=-20, type=int)
@click.option('--wiggle', '-W', default=1.0, type=float)
@click.option('--accurate', '-A', is_flag=True)
@click.argument('dbpath', type=click.Path())
@click.argument('query', nargs=-1)
def search(dbpath, query, upload=False, image=None, rand=False, webm=False, noise=-20, wiggle=1.0, accurate=False):
    if isinstance(query, (list, tuple)):
        query = ' '.join(query)
    if image is None:
        image = query.strip().replace(' ', '+') + ('.webm' if webm else '.png')
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
        click.echo('Content: {}'.format(' \\ '.join(line.strip()
            for line in ev.content.strip().splitlines())))

        if not rand:
            base, ext = os.path.splitext(image)
            image_fn = '%s%03d%s' % (base, i, ext)

        if webm:
            if not accurate:
                silences = []
            else:
                try:
                    click.echo('Finding silences for webm clipping, this will take some time')
                    silences = ff.read_silences(ev.path, noise=noise)
                except ValueError:
                    # silently fall back to using subtitle event times
                    silences = []
            start, duration = get_clip_times(ev, silences, wiggle)
            click.echo('Rendering webm clip, this will also take some time')
            ff.get_clip(ev.path, start, duration, image_fn)
        else:
            ff.get_image(ev.path, ev.start, ev.midpoint, image_fn)
        click.echo('Image: {}'.format(image_fn))

        if upload:
            do_upload(image_fn)

def get_clip_times(event, silences, wiggle=1.0):
    ev_start = event.start / 1000
    ev_end = event.end / 1000
    pre_silence = post_silence = None
    clip_start = ev_start - (wiggle / 2)
    clip_duration = (ev_end - ev_start) + (wiggle / 2)

    for (start, end, dur) in silences:
        # find preceding silence
        if ev_start - wiggle <= end and \
                end <= ev_start + wiggle:
            pre_silence = (start, end, dur)
            break
    for (start, end, dur) in silences[::-1]:
        # find following silence
        if ev_end - wiggle <= start and \
                start <= ev_end + wiggle:
            post_silence = (start, end, dur)
            break

    if pre_silence is not None:
        clip_start = max(pre_silence[0] + (pre_silence[2]/2), ev_start - wiggle)
    if post_silence is not None:
        clip_duration = min((post_silence[0] + (post_silence[2]/2)) - clip_start, ev_end + wiggle)

    if clip_duration < 0:
        raise ValueError('Invalid clip duration selected: {}'.format(clip_duration))

    return clip_start, clip_duration


if __name__ == "__main__":
    cli()
