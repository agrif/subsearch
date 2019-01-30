#!/usr/bin/env python3
import subprocess
import json
import os
import os.path
import random
import tempfile
import re
import glob
import hashlib
import gzip

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
        run_args = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.DEVNULL,
            'check': True,
        }
        run_args.update(kwargs)
        args_actual = [
            self.cmd, '-nostdin', '-y', '-hide_banner',
            '-loglevel', 'panic', '-nostats'] + list(args)
        # click.echo('# ' + ' '.join(args_actual), err=True)
        return subprocess.run(args_actual, **run_args)

    def read_streams(self, path):
        out = subprocess.run(
            [self.cmd.replace('ffmpeg', 'ffprobe'), '-hide_banner',
                '-i', path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE).stderr.decode('utf-8')
        streams = [{'stream_id': m[0], 'stream_lang': m[1], 'stream_type': m[2], 'stream_format': m[3]} \
            for m in re.findall(r'Stream #\d+:(?P<stream_id>\d+)(?:\((?P<stream_lang>\w+)\))?: (?P<stream_type>\w+): (?P<stream_format>.*)', out)]
        return streams

    def read_subs(self, path):
        def get_sub_track(track_id):
            return self.run('-i', path, '-map', '0:'+track_id, '-f', 'ass', '-').stdout.decode('utf-8')
        sub_tracks = sorted(
            (get_sub_track(strm['stream_id']) for strm in self.read_streams(path) \
                if strm['stream_type'].lower() == 'subtitle' \
                    and strm['stream_lang'].lower() in ('', 'eng', 'und')),
            key=lambda st: len(st))
        try:
            # return longest subtitle track
            return sub_tracks[-1]
        except IndexError:
            raise ValueError('No subtitle tracks found')

    def read_duration(self, path):
        out = subprocess.run(
            [self.cmd.replace('ffmpeg', 'ffprobe'), '-hide_banner',
                '-i', path, '-show_format'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode('utf-8')
        duration = float(re.search(r'duration=([\d.]+)', out).group(1))

        return duration

    def read_volume_stats(self, path):
        # use the duration to skip the first and last 20% of the file, as a
        #   quick and dirty way to try to skip OP and ED so they don't affect the
        #   volume analysis
        dur = self.read_duration(path)
        try:
            out = self.run('-loglevel', 'info',
                '-ss', str(dur * 0.2),
                '-i', path,
                '-t', str(dur * 0.6),
                '-vn', '-filter_complex', '[0:m:language:jpn]volumedetect[a_out]',
                '-map', '[a_out]',
                '-f', 'null',
                '-',
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE).stderr.decode('utf-8')
        except subprocess.CalledProcessError:
            out = self.run('-loglevel', 'info',
                '-ss', str(dur * 0.2),
                '-i', path,
                '-t', str(dur * 0.6),
                '-vn', '-af', 'volumedetect',
                '-f', 'null',
                '-',
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE).stderr.decode('utf-8')

        mean_vol = float(re.search(r'mean_volume: ([\d.-]+)', out).group(1))
        max_vol = float(re.search(r'max_volume: ([\d.-]+)', out).group(1))

        return mean_vol, max_vol

    def read_silences(self, path, noise=-30, duration=0.3):
        # need loglevel=info here because silencedetect outputs via log and
        #   loglevel=panic is set by run()
        try:
            out = self.run('-loglevel', 'info',
                '-i', path,
                '-vn', '-filter_complex', '[0:m:language:jpn]silencedetect=noise={:1.1f}dB:d={:1.2f}[a_out]'.format(noise, duration),
                '-map', '[a_out]',
                '-f', 'null',
                '-',
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE).stderr.decode('utf-8')
        except subprocess.CalledProcessError:
            out = self.run('-loglevel', 'info',
                '-i', path,
                '-vn', '-af', 'silencedetect=noise={:1.1f}dB:d={:1.2f}'.format(noise, duration),
                '-f', 'null',
                '-',
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE).stderr.decode('utf-8')


        silence_starts = re.findall(r'silence_start:\s+([\d.e+-]+)', out)
        silence_ends = re.findall(r'silence_end:\s+([\d.e+-]+)', out)
        silence_durations = re.findall(r'silence_end.*silence_duration:\s+([\d.e+-]+)', out)

        if len(silence_starts) != len(silence_ends) or len(silence_starts) != len(silence_durations):
            raise ValueError('Non-matching length of silence detections: starts=%d ends=%d dur=%d' % 
                (len(silence_starts), len(silence_ends), len(silence_durations)))

        return sorted(zip(map(float, silence_starts), map(float, silence_ends), map(float, silence_durations)), key=lambda s: s[0])

    def get_clip(self, path, start, time, name):
        with tempfile.NamedTemporaryFile(suffix='.ass') as temp_f:
            temp_f.write(self.read_subs(path).encode('utf-8'))
            temp_f.flush()
            try:
                for p in range(1, 3):
                    self.run(
                        '-y',
                        '-ss', str(start),
                        '-i', path,
                        '-copyts',
                        '-sn',
                        '-t', str(time),
                        '-filter_complex', "[0:V]subtitles='{}',setpts=PTS-STARTPTS[v0];[0:m:language:jpn]asetpts=PTS-STARTPTS,aformat=channel_layouts=stereo[a0]".format(
                            temp_f.name.replace("'", r"\'").replace(':', r'\:')),
                        '-map', '[v0]', '-map', '[a0]',
                        '-c:v', 'libvpx-vp9',
                        '-crf', '15',
                        '-b:v', '0',
                        '-c:a', 'libopus',
                        '-b:a', '128k',
                        '-pass', str(p),
                        '-f', 'webm',
                        name)
            except subprocess.CalledProcessError as err:
                try:
                    for p in range(1, 3):
                        self.run(
                            '-y',
                            '-ss', str(start),
                            '-i', path,
                            '-copyts',
                            '-sn',
                            '-t', str(time),
                            '-filter_complex', "[0:V]subtitles='{}',setpts=PTS-STARTPTS[v0];[0:a]asetpts=PTS-STARTPTS,aformat=channel_layouts=stereo[a0]".format(
                                temp_f.name.replace("'", r"\'").replace(':', r'\:')),
                            '-map', '[v0]', '-map', '[a0]',
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
                            '-copyts',
                            '-sn',
                            '-t', str(time),
                            '-filter_complex', '[0:V][0:s]overlay[v_out];[0:a]asetpts=PTS-STARTPTS,aformat=channel_layouts=stereo[a0]',
                            '-map', '[v_out]', '-map', '[a0]',
                            '-c:v', 'libvpx-vp9',
                            '-crf', '15',
                            '-b:v', '0',
                            '-c:a', 'libopus',
                            '-b:a', '128k',
                            '-pass', str(p),
                            '-f', 'webm',
                            name)
            finally:
                # remove ffmpeg pass log file if it exists
                for fn in glob.glob('ffmpeg2pass-*.log'):
                    try:
                        os.unlink(fn)
                    except os.error: pass

    def get_image(self, path, start, time, name):
        try:
            with tempfile.NamedTemporaryFile(suffix='.ass') as temp_f:
                temp_f.write(self.read_subs(path).encode('utf-8'))
                temp_f.flush()
                self.run(
                    '-ss', str(start / 1000),
                    '-i', path,
                    '-copyts',
                    '-an',
                    '-ss', str(time / 1000),
                    '-filter_complex', "subtitles='{}'".format(
                        temp_f.name.replace("'", r"\'").replace(':', r'\:')),
                    '-vframes', '1',
                    '-f', 'image2',
                    name)
        except subprocess.CalledProcessError as err:
            self.run(
                '-ss', str(start / 1000),
                '-i', path,
                '-copyts',
                '-an',
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
class Cache:
    path = attr.ib(default='.cache')

    @classmethod
    def open(cls, path):
        os.makedirs(path, mode=0o770, exist_ok=True)
        return cls(path)

    def get(self, key, miss=None):
        p = self._normalize_key(key)
        try:
            with gzip.open(p, 'rt') as cf:
                value = json.load(cf)
        except os.error:
            try:
                value = miss()
                self.set(key, value)
            except TypeError:
                value = miss
        return value


    def set(self, key, value):
        with gzip.open(self._normalize_key(key), 'wt') as cf:
            json.dump(value, cf)
        return value

    def pop(self, key):
        value = self.get(key)
        try:
            os.unlink(self._normalize_key(key))
        except os.error: pass
        return value

    def _normalize_key(self, key):
        return os.path.join(self.path, '{}.json.gz'.format(
            hashlib.sha1(json.dumps(key).encode('utf-8')).hexdigest()))

@attr.s
class Database:
    CACHE_DIR_NAME = 'audio-cache'

    path = attr.ib()
    ix = attr.ib()
    relative = attr.ib()
    cache = attr.ib()
    
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
        cache = Cache.open(os.path.join(path, cls.CACHE_DIR_NAME))
        return cls(path, ix, relative, cache)

    @classmethod
    def open(cls, path):
        ix = whoosh.index.open_dir(path)
        with open(os.path.join(path, 'subsearch-config.json')) as f:
            config = json.load(f)
        cache = Cache.open(os.path.join(path, cls.CACHE_DIR_NAME))
        return cls(path, ix, cache=cache, **config)

    def search(self, query, **kwargs):
        q = whoosh.qparser.QueryParser("content", self.ix.schema).parse(query)
        with self.ix.searcher() as searcher:
            for r in searcher.search(q, **kwargs):
                d = dict(r)
                d['path'] = os.path.normpath(os.path.join(self.path, d['path']))
                yield Result(**d)

    def apply_recursive(self, method, path, *args, **kwargs):
        for d in sorted(os.listdir(path)):
            full = os.path.join(path, d)
            method(full, *args, **kwargs)

    def add(self, path, ff, report=None, relative=None, process_audio=False, wiggle=1.0):
        if relative is None:
            relative = self.relative
        if os.path.isdir(path):
            return self.apply_recursive(self.add, path, ff, report=report, relative=relative, process_audio=process_audio)

        realpath = path
        if relative:
            path = os.path.normpath(os.path.relpath(path, self.path))
        else:
            path = os.path.abspath(path)
        if report:
            report(path)

        try:
            subs = pysubs2.SSAFile.from_string(ff.read_subs(realpath))
        except (subprocess.CalledProcessError, ValueError):
            if report:
                report("!!! Error extracting subtitles...")
            return

        writer = self.ix.writer()
        for ev in subs.events:
            if ev.is_comment:
                continue
            writer.add_document(path=path, start=ev.start, end=ev.end, content=ev.plaintext)
        writer.commit()

        if process_audio:
            mean_vol, _ = self.cache.set((path, 'volume_stats'),
                ff.read_volume_stats(path))
            self.cache.set((path, 'silences'),
                ff.read_silences(path, noise=mean_vol * 0.9, duration=0.3 * wiggle))

    def remove(self, path, report=None, relative=None):
        if relative is None:
            relative = self.relative
        if os.path.isdir(path):
            return self.apply_recursive(self.remove, path, report=report, relative=relative)

        realpath = path
        if relative:
            path = os.path.normpath(os.path.relpath(path, self.path))
        else:
            path = os.path.abspath(path)
        if report:
            report(path)

        writer = self.ix.writer()
        writer.delete_by_term('path', path)
        writer.commit()

        self.cache.pop((path, 'volume_stats'))
        self.cache.pop((path, 'silences'))


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
@click.option('--wiggle', '-W', default=1.0, type=float)
@click.option('--audio', '-A', is_flag=True)
@click.argument('dbpath', type=click.Path())
@click.argument('paths', nargs=-1, type=click.Path(exists=True))
def add(dbpath, paths, relative, audio=False, wiggle=1.0):
    db = Database.open(dbpath)
    ff = FFmpeg('ffmpeg')
    def report(s):
        click.echo('adding: {}'.format(s))
    for path in paths:
        db.add(path, ff, report=report, relative=relative, process_audio=audio, wiggle=wiggle)

@cli.command()
@click.option('--relative/--absolute', '-r/-a', is_flag=True, default=None)
@click.argument('dbpath', type=click.Path())
@click.argument('paths', nargs=-1, type=click.Path(exists=True))
def rm(dbpath, paths, relative):
    db = Database.open(dbpath)
    def report(s):
        click.echo('removing: {}'.format(s))
    for path in paths:
        db.remove(path, report=report, relative=relative)

@cli.command()
@click.option('--image', '-i', type=click.Path())
@click.option('--rand', '-R', is_flag=True)
@click.option('--upload', '-u', is_flag=True)
@click.option('--webm', '-w', is_flag=True)
@click.option('--wiggle', '-W', default=1.0, type=float)
@click.option('--accurate', '-A', is_flag=True)
@click.argument('dbpath', type=click.Path())
@click.argument('query', nargs=-1)
def search(dbpath, query, upload=False, image=None, rand=False, webm=False, wiggle=1.0, accurate=False):
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
            silences = []
            if accurate:
                click.echo('Finding silences for accurate clipping, this will take some time')
                try:
                    mean_vol, _ = db.cache.get((ev.path, 'volume_stats'),
                        lambda: ff.read_volume_stats(ev.path))
                    silences = db.cache.get((ev.path, 'silences'),
                        lambda: ff.read_silences(ev.path, noise=mean_vol * 0.9, duration=0.3 * wiggle))
                except ValueError:
                    # silently fall back to using subtitle event times
                    pass

            start, duration = get_clip_times(ev, silences, wiggle)
            click.echo('Adjusted clip start/end by: {:1.03f} / {:1.03f}'.format(
                start - ev.start / 1000,
                (start + duration) - ev.end / 1000))
            click.echo('Rendering webm clip, this will take some time')
            ff.get_clip(ev.path, start, duration, image_fn)
        else:
            ff.get_image(ev.path, ev.start, ev.midpoint, image_fn)
        click.echo('Image: {}'.format(image_fn))

        if upload:
            do_upload(image_fn)

def get_clip_times(event, silences, wiggle=1.0):
    ev_start = event.start / 1000
    ev_end = event.end / 1000
    clip_start = ev_start - (wiggle / 2)
    clip_duration = (ev_end - ev_start) + wiggle

    for (start, end, dur) in silences:
        # find preceding silence
        if ev_start - wiggle <= end and \
                end <= ev_start + wiggle:

            clip_start = max(
                end - min(dur / 3, wiggle / 2),
                ev_start - wiggle,
                0)
            break

    for (start, end, dur) in silences[::-1]:
        # find following silence
        if ev_end - wiggle <= start and \
                start <= ev_end + wiggle:

            clip_duration = max(
                min(
                    (start + (min(dur / 3, wiggle / 2))) - clip_start,
                    (ev_end + wiggle) - clip_start),
                0)
            break

    return clip_start, clip_duration


if __name__ == "__main__":
    cli()
