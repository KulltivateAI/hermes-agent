"""Real independent SQLite connections: atomic comparison and multi-key rollback."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest
from hermes_state import SessionDB


def test_competing_insert_and_two_key_transfer(tmp_path):
    a = SessionDB(db_path=tmp_path / 'state.db')
    b = SessionDB(db_path=tmp_path / 'state.db')
    barrier = threading.Barrier(2)
    def insert(pair):
        db, value = pair
        barrier.wait(timeout=10)
        return db.compare_and_set_meta({'old': (None, value)}, patience_s=0.5)
    try:
        with ThreadPoolExecutor(2) as pool:
            wins = list(pool.map(insert, [(a, 'a'), (b, 'b')]))
        assert sorted(wins) == [False, True]
        raw = a.get_meta('old')
        assert not b.compare_and_set_meta({'old': ('stale', 'cleared'), 'new': (None, 'copy')})
        assert a.get_meta('old') == raw and a.get_meta('new') is None
        assert b.compare_and_set_meta({'old': (raw, 'cleared'), 'new': (None, 'copy')})
        assert a.get_meta('old') == 'cleared' and a.get_meta('new') == 'copy'
        assert not a.compare_and_set_meta({'old': (None, 'ABA')})
    finally:
        a.close(); b.close()


def test_callback_error_rolls_back_all_keys(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    original = db.set_meta
    def fail_second(key, value, *, cursor=None):
        if key == 'b':
            raise sqlite3.OperationalError('injected IO failure')
        return original(key, value, cursor=cursor)
    monkeypatch.setattr(db, 'set_meta', fail_second)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.compare_and_set_meta({'a': (None, '1'), 'b': (None, '2')})
        assert db.get_meta('a') is None and db.get_meta('b') is None
    finally:
        db.close()


def _process_insert(path, ready, start, results, value):
    db = SessionDB(db_path=path)
    try:
        ready.put(True)
        assert start.wait(10)
        results.put(db.compare_and_set_meta({'process':(None,value)}, patience_s=0.5))
    finally:
        db.close()


def test_process_insert_winner_and_native_lock_contention(tmp_path):
    import multiprocessing
    path = tmp_path / 'process.db'
    db = SessionDB(db_path=path)
    ctx = multiprocessing.get_context('spawn')
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    children = [ctx.Process(target=_process_insert,args=(path,ready,start,results,str(i))) for i in range(2)]
    try:
        for child in children: child.start()
        for _ in children: assert ready.get(timeout=15)
        start.set()
        assert sorted(results.get(timeout=15) for _ in children) == [False,True]
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        assert db.get_meta('process') in {'0','1'}
        # Native retry-patience is not a wall-clock deadline. Force a controlled immediate lock error.
        blocker = sqlite3.connect(path)
        blocker.execute('BEGIN IMMEDIATE')
        db._conn.execute('PRAGMA busy_timeout=0')
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                db.compare_and_set_meta({'locked':(None,'bad')}, patience_s=0)
        finally:
            blocker.rollback(); blocker.close()
        assert db.get_meta('locked') is None
        assert db.compare_and_set_meta({'locked':(None,'recovered')}, patience_s=0.5)
    finally:
        start.set()
        for child in children:
            if child.pid is not None: child.join(15)
        db.close()
