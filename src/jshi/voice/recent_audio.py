"""Bounded, durable microphone audio and per-input diagnostic evidence."""
from __future__ import annotations

import atexit
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from threading import Condition, RLock, Thread
from time import sleep, time
import wave
from contextlib import contextmanager


class RecentAudio:
    chunk_bytes = 60 * 32000

    def __init__(self, root, *, hours=24, max_bytes=512*1024*1024):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'index.sqlite3'
        self.hours, self.max_bytes = hours, max_bytes
        self.lock = RLock()
        self._last_prune = 0
        self._pending = []
        self._writing = False
        self._queued = Condition()
        self._writer = None
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chunks(
                    capture TEXT, start INTEGER, size INTEGER, updated REAL,
                    PRIMARY KEY(capture,start));
                CREATE TABLE IF NOT EXISTS inputs(
                    id TEXT PRIMARY KEY, capture TEXT, start_ms INTEGER, end_ms INTEGER,
                    created REAL, metadata TEXT);
            ''')
        self.prune()

    @contextmanager
    def _db(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _file(self, capture, start):
        # Callers cannot turn capture / input IDs into paths outside this directory.
        digest = hashlib.sha256(str(capture).encode()).hexdigest()
        return self.root/f'{digest}-{int(start)}.pcm'

    def append(self, capture, offset, pcm):
        if len(pcm) % 2:
            raise ValueError('音频保存需要16位PCM')
        with self.lock, self._db() as db:
            cursor = 0
            while cursor < len(pcm):
                position = offset+cursor
                start = position//self.chunk_bytes*self.chunk_bytes
                within = position-start
                piece = pcm[cursor:cursor+min(len(pcm)-cursor,self.chunk_bytes-within)]
                path = self._file(capture,start)
                with path.open('r+b' if path.exists() else 'w+b') as handle:
                    handle.seek(within)
                    handle.write(piece)
                    handle.flush()
                db.execute('INSERT INTO chunks VALUES(?,?,?,?) ON CONFLICT(capture,start) DO UPDATE SET '
                    'size=MAX(size,excluded.size),updated=excluded.updated',
                    (capture,start,within+len(piece),time()))
                cursor += len(piece)
        if time()-self._last_prune >= 30:
            self.prune()

    def enqueue(self, capture, offset, pcm, on_error=None):
        """Queue live microphone audio; the receive loop must never wait on disk."""
        if len(pcm) % 2:
            raise ValueError('音频保存需要16位PCM')
        with self._queued:
            last = self._pending[-1] if self._pending else None
            if last and last[0] == capture and last[1]+len(last[2]) == offset and last[3] == on_error:
                last[2].extend(pcm)
            else:
                self._pending.append((capture, offset, bytearray(pcm), on_error))
            if self._writer is None:
                self._writer = Thread(target=self._write_pending, name='jshi-recent-audio', daemon=True)
                self._writer.start()
                atexit.register(self.flush)
            self._queued.notify_all()

    def _write_pending(self):
        while True:
            with self._queued:
                while not self._pending:
                    self._queued.wait()
            sleep(.25)  # Gather several 100 ms frames into one commit.
            with self._queued:
                batch, self._pending, self._writing = self._pending, [], True
            for capture, offset, pcm, on_error in batch:
                try:
                    self.append(capture, offset, bytes(pcm))
                except (OSError, sqlite3.Error) as exc:
                    if on_error:
                        on_error(str(exc))
            with self._queued:
                self._writing = False
                self._queued.notify_all()

    def flush(self, timeout=5.0):
        with self._queued:
            return self._queued.wait_for(lambda: not self._pending and not self._writing, timeout)

    def segment(self,capture,start_ms,end_ms):
        begin,end = int(start_ms)*32,int(end_ms)*32
        if begin < 0 or end <= begin:
            return None
        self.flush()
        with self.lock, self._db() as db:
            chunks = db.execute('SELECT start,size FROM chunks WHERE capture=? AND start<? AND start+size>? '
                                'ORDER BY start',(capture,end,begin)).fetchall()
            pieces, cursor = [], begin
            for start,size in chunks:
                if start > cursor:
                    return None
                stop = min(end,start+size)
                if stop <= cursor:
                    continue
                try:
                    with self._file(capture,start).open('rb') as handle:
                        handle.seek(cursor-start)
                        piece = handle.read(stop-cursor)
                except FileNotFoundError:
                    return None
                if len(piece) != stop-cursor:
                    return None
                pieces.append(piece);cursor=stop
            return b''.join(pieces) if cursor == end else None

    def save_input(self,iid,capture,transcript,*,pcm=None,received_at_ms=None):
        metadata = {'transcript':transcript,'received_at_ms':received_at_ms,
            'format':{'sample_rate':16000,'channels':1,'sample_width':2},
            'classification_pcm_sha256':hashlib.sha256(pcm).hexdigest() if pcm else None,
            'classification_pcm_bytes':len(pcm) if pcm else 0}
        with self.lock,self._db() as db:
            db.execute('INSERT OR IGNORE INTO inputs VALUES(?,?,?,?,?,?)',
                (iid,capture,transcript['start_ms'],transcript['end_ms'],time(),json.dumps(metadata,ensure_ascii=False)))

    def update(self,iid,**values):
        with self.lock,self._db() as db:
            row=db.execute('SELECT metadata FROM inputs WHERE id=?',(iid,)).fetchone()
            if row:
                metadata=json.loads(row[0]);metadata.update(values)
                db.execute('UPDATE inputs SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False,allow_nan=False),iid))

    def get(self,iid):
        with self.lock,self._db() as db:
            row=db.execute('SELECT id,capture,start_ms,end_ms,created,metadata FROM inputs WHERE id=? AND created>=?',
                (iid,time()-self.hours*3600)).fetchone()
        if not row:
            return None
        keys=('input_id','capture','start_ms','end_ms','created','metadata')
        result=dict(zip(keys,row));result['metadata']=json.loads(result['metadata'])
        return result

    def recent(self,limit=100):
        self.prune()
        with self.lock,self._db() as db:
            ids=[r[0] for r in db.execute('SELECT id FROM inputs ORDER BY created DESC LIMIT ?',(limit,))]
        rows=[row for iid in ids if (row:=self.get(iid)) is not None]
        for row in rows:
            row['audio_available']=self.segment(row['capture'],row['start_ms'],row['end_ms']) is not None
        return rows

    def wav(self,iid,*,padding_ms=0):
        row=self.get(iid)
        if not row:
            return None
        start=max(0,row['start_ms']-padding_ms)
        end=row['end_ms']+padding_ms
        pcm=self.segment(row['capture'],start,end)
        if pcm is None and padding_ms:
            pcm=self.segment(row['capture'],row['start_ms'],row['end_ms'])
        if pcm is None:
            return None
        output=io.BytesIO()
        with wave.open(output,'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(16000);handle.writeframes(pcm)
        return output.getvalue()

    def continuous(self,limit=20):
        self.flush()
        with self.lock,self._db() as db:
            return [dict(zip(('capture','start','size','updated'),r)) for r in db.execute(
                'SELECT capture,start,size,updated FROM chunks ORDER BY updated DESC LIMIT ?',(limit,))]

    def stream_wav(self,capture,start):
        self.flush()
        with self.lock,self._db() as db:
            row=db.execute('SELECT size FROM chunks WHERE capture=? AND start=? AND updated>=?',
                (capture,start,time()-self.hours*3600)).fetchone()
            if not row:
                return None
            try:
                with self._file(capture,start).open('rb') as handle:pcm=handle.read(row[0])
            except FileNotFoundError:
                return None
        output=io.BytesIO()
        with wave.open(output,'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(16000);handle.writeframes(pcm)
        return output.getvalue()

    def prune(self):
        with self.lock,self._db() as db:
            chunks=db.execute('SELECT capture,start,size,updated FROM chunks ORDER BY updated').fetchall()
            total=sum(r[2] for r in chunks)
            cutoff=time()-self.hours*3600
            for capture,start,size,updated in chunks:
                if updated >= cutoff and total <= self.max_bytes:
                    break
                self._file(capture,start).unlink(missing_ok=True)
                db.execute('DELETE FROM chunks WHERE capture=? AND start=?',(capture,start))
                total-=size
            db.execute('DELETE FROM inputs WHERE created<?',(cutoff,))
            db.execute('DELETE FROM inputs WHERE id IN (SELECT id FROM inputs ORDER BY created DESC LIMIT -1 OFFSET 5000)')
            self._last_prune=time()
