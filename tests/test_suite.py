#!/usr/bin/env python3
"""Zappy - complete test battery, organised by section of the subject.

    python3 tests/test_suite.py            # everything
    python3 tests/test_suite.py -k Broadcast -v
    ZAPPY_SKIP_SLOW=1 python3 tests/test_suite.py   # skip the long end-to-end games

Only the Python standard library is needed. The graphic client tests need a
Python with tkinter and a display; they are skipped otherwise.
"""

import glob
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zappy_testlib import (CLIENT, DIRS, ELEVATION, GFX, ROOT, SERVER, STONES, Closed, Conn, Dead,  # noqa: E402
                           GfxObserver, Player, ServerProc, cpu_seconds, expected_sound_k, join_player,
                           parse_vision, shortest, thread_count, vision_squares)

SLOW = os.environ.get('ZAPPY_SKIP_SLOW') != '1'
INVENTORY_RE = re.compile(r'^\{nourriture \d+, linemate \d+, deraumere \d+, sibur \d+, '
                          r'mendiane \d+, phiras \d+, thystame \d+\}$')


def setUpModule():
    subprocess.run(['make', '-s', 'all'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


class ZappyCase(unittest.TestCase):
    """Starts servers / graphic observers and always cleans them up."""

    def setUp(self):
        self._servers, self._conns = [], []

    def tearDown(self):
        for c in self._conns:
            c.close()
        for s in self._servers:
            s.__exit__(None, None, None)

    def server(self, **kw):
        srv = ServerProc(**kw)
        self._servers.append(srv)
        self.assertTrue(srv.ready, f'server did not start:\n{srv.output()[-2000:]}')
        return srv

    def gfx(self, srv):
        g = GfxObserver(srv.port)
        self._conns.append(g)
        self.assertTrue(g.ready(), 'graphic client did not receive the whole map')
        return g

    def player(self, srv, gfx, team='team1'):
        p = join_player(srv, gfx, team)
        self._conns.append(p)
        return p

    def raw(self, srv):
        c = Conn(srv.port)
        self._conns.append(c)
        return c

    def sync(self, g, p):
        """Takes the position of `p` from the graphic model (after raw commands)."""
        time.sleep(0.1)
        with g.lock:
            q = g.players[p.id]
            p.x, p.y, p.dir = q['x'], q['y'], q['dir']

    def assertDuration(self, elapsed, units, t, msg=''):
        expected = units / t
        self.assertGreaterEqual(elapsed, expected * 0.85 - 0.005, f'{msg}: too fast ({elapsed:.3f}s < {expected:.3f}s)')
        self.assertLess(elapsed, expected + 0.08, f'{msg}: too slow ({elapsed:.3f}s > {expected:.3f}s)')


# =============================================================================
# Chapter V - Submission: Makefile, binaries, forbidden functions
# =============================================================================
class T01_Makefile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='zappy_make_')
        for item in ('Makefile', 'srcs', 'includes'):
            src = os.path.join(ROOT, item)
            (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(self.dir, item))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, *args):
        return subprocess.run(['make', *args], cwd=self.dir, capture_output=True, text=True)

    def test_required_rules_exist(self):
        for rule in ('client', 'server', 'gfx', 'all', 'clean', 'fclean', 're'):
            with self.subTest(rule=rule):
                self.assertEqual(self.make('-n', rule).returncode, 0, f'rule {rule} missing')

    def test_all_builds_three_executables(self):
        self.assertEqual(self.make('all').returncode, 0)
        for name in ('server', 'client', 'gfx'):
            path = os.path.join(self.dir, name)
            self.assertTrue(os.path.isfile(path) and os.access(path, os.X_OK), f'{name} not executable')

    def test_no_relink_when_up_to_date(self):
        self.make('all')
        self.assertEqual(self.make('-q', 'all').returncode, 0, 'make thinks something must be rebuilt')
        out = self.make('all').stdout
        self.assertNotIn('c++', out)
        self.assertNotIn('cp ', out)

    def test_header_change_recompiles(self):
        self.make('all')
        time.sleep(1.1)
        os.utime(os.path.join(self.dir, 'includes', 'Player.hpp'))
        out = self.make('all').stdout
        self.assertIn('Player.o', out)
        self.assertIn('-o server', out)
        self.assertNotIn('cp srcs/client', out)

    def test_script_change_only_reinstalls_script(self):
        self.make('all')
        time.sleep(1.1)
        os.utime(os.path.join(self.dir, 'srcs', 'client', 'client.py'))
        out = self.make('all').stdout
        self.assertIn('client', out)
        self.assertNotIn('c++', out)

    def test_individual_rules(self):
        for rule in ('server', 'client', 'gfx'):
            with self.subTest(rule=rule):
                self.assertEqual(self.make(rule).returncode, 0)
                self.assertTrue(os.path.isfile(os.path.join(self.dir, rule)))

    def test_clean_fclean_re(self):
        self.make('all')
        self.make('clean')
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'obj')))
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'server')))
        self.make('fclean')
        for name in ('server', 'client', 'gfx'):
            self.assertFalse(os.path.exists(os.path.join(self.dir, name)))
        self.assertEqual(self.make('re').returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'server')))

    def test_strict_flags(self):
        with open(os.path.join(self.dir, 'Makefile')) as f:
            content = f.read()
        for flag in ('-Wall', '-Wextra', '-Werror'):
            self.assertIn(flag, content)


class T02_ForbiddenFunctions(unittest.TestCase):
    def server_sources(self):
        files = glob.glob(os.path.join(ROOT, 'srcs', '**', '*.cpp'), recursive=True)
        files += glob.glob(os.path.join(ROOT, 'includes', '*.hpp'))
        for path in files:
            with open(path) as f:
                yield path, f.read()

    def test_no_thread_no_nonblocking_socket_in_server(self):
        forbidden = ['std::thread', 'pthread', 'fcntl', 'O_NONBLOCK', 'MSG_DONTWAIT', 'SOCK_NONBLOCK', 'fork(']
        for path, src in self.server_sources():
            for word in forbidden:
                self.assertNotIn(word, src, f'{word} used in {os.path.relpath(path, ROOT)}')

    def test_server_binary_does_not_create_threads(self):
        out = subprocess.run(['nm', '-D', '--undefined-only', SERVER], capture_output=True, text=True).stdout
        self.assertNotIn('pthread_create', out)
        self.assertNotIn('fcntl', out)

    def test_server_runs_single_threaded(self):
        with ServerProc() as srv:
            self.assertTrue(srv.ready)
            g = GfxObserver(srv.port)
            p = join_player(srv, g)
            p.cmd('voir')
            self.assertEqual(thread_count(srv.pid), 1)
            p.close()
            g.close()

    def test_server_is_compiled(self):
        with open(SERVER, 'rb') as f:
            self.assertEqual(f.read(4), b'\x7fELF')


# =============================================================================
# III.1 / III.2 - Usage of the programs
# =============================================================================
class T03_ServerArguments(ZappyCase):
    BAD = {
        'no argument': [],
        'missing port': ['-x', '5', '-y', '5', '-n', 'a', '-c', '1'],
        'port 0': ['-p', '0', '-x', '5', '-y', '5', '-n', 'a', '-c', '1'],
        'port too big': ['-p', '70000', '-x', '5', '-y', '5', '-n', 'a', '-c', '1'],
        'port not a number': ['-p', 'abc', '-x', '5', '-y', '5', '-n', 'a', '-c', '1'],
        'width 0': ['-p', '4242', '-x', '0', '-y', '5', '-n', 'a', '-c', '1'],
        'negative height': ['-p', '4242', '-x', '5', '-y', '-5', '-n', 'a', '-c', '1'],
        'no team': ['-p', '4242', '-x', '5', '-y', '5', '-c', '1'],
        '-n without team': ['-p', '4242', '-x', '5', '-y', '5', '-n', '-c', '1'],
        'duplicate team': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', 'a', '-c', '1'],
        'reserved team GRAPHIC': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'GRAPHIC', '-c', '1'],
        'clients 0': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', '-c', '0'],
        't 0': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', '-c', '1', '-t', '0'],
        't not a number': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', '-c', '1', '-t', 'fast'],
        't overflow': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', '-c', '1', '-t', '99999999999999'],
        'duplicate flag': ['-p', '4242', '-p', '4243', '-x', '5', '-y', '5', '-n', 'a', '-c', '1'],
        'unknown flag': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', '-c', '1', '-z', '3'],
        'missing value': ['-p', '4242', '-x', '5', '-y', '5', '-n', 'a', '-c'],
    }

    def test_invalid_arguments_are_rejected_cleanly(self):
        for name, args in self.BAD.items():
            with self.subTest(case=name):
                r = subprocess.run([SERVER, *args], capture_output=True, text=True, timeout=5)
                self.assertGreater(r.returncode, 0, f'{name}: exit code {r.returncode}')
                self.assertIn('Usage', r.stderr + r.stdout)

    def test_help(self):
        r = subprocess.run([SERVER, '--help'], capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0)
        self.assertIn('-p port number', r.stdout)

    def test_default_t_is_100(self):
        srv = self.server(t=None)
        self.assertIn('t: 100', srv.output())
        g = self.gfx(srv)
        self.assertEqual(g.t, 100)
        p = self.player(srv, g)
        _, elapsed = p.timed('avance')
        self.assertDuration(elapsed, 7, 100, 'avance with the default t')

    def test_several_teams_and_any_flag_order(self):
        port = __import__('zappy_testlib').free_port()
        srv = self.server(port=port, args=['-c', '2', '-n', 'alpha', 'beta', 'gamma', '-t', '50', '-y', '7',
                                           '-x', '9', '-p', str(port)])
        g = self.gfx(srv)
        self.assertEqual(g.teams, ['alpha', 'beta', 'gamma'])
        self.assertEqual((g.w, g.h, g.t), (9, 7, 50))

    def test_port_already_used(self):
        srv = self.server()
        r = subprocess.run([SERVER, '-p', str(srv.port), '-x', '5', '-y', '5', '-n', 'a', '-c', '1'],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 1)
        self.assertIn('bind', r.stderr + r.stdout)
        self.assertTrue(srv.alive())

    def test_signals_stop_the_server_cleanly(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig.name):
                with ServerProc() as srv:
                    g = GfxObserver(srv.port)
                    join_player(srv, g)
                    self.assertEqual(srv.stop(sig), 0)
                    g.close()


# =============================================================================
# III.5 - Client/server communication (handshake)
# =============================================================================
class T04_Handshake(ZappyCase):
    def test_welcome_then_slots_then_dimensions(self):
        srv = self.server(x=12, y=8, c=3)
        p = Player(srv.port)
        self._conns.append(p)
        self.assertEqual(p.join('team1'), ('BIENVENUE', '2', '12 8'))

    def test_slots_decrease_then_team_is_full(self):
        srv = self.server(c=3)
        for expected in ('2', '1', '0'):
            p = Player(srv.port)
            self._conns.append(p)
            self.assertEqual(p.join('team1')[1], expected)
        p = Player(srv.port)
        self._conns.append(p)
        self.assertEqual(p.join('team1')[1], 'ko')
        # the other team still has all its slots
        p = Player(srv.port)
        self._conns.append(p)
        self.assertEqual(p.join('team2')[1], '2')

    def test_unknown_team_then_retry(self):
        srv = self.server()
        c = self.raw(srv)
        self.assertEqual(c.readline(), 'BIENVENUE')
        for bad in ('nope', 'TEAM1', 'team1 ', ''):
            c.send(bad + '\n')
            self.assertEqual(c.readline(), 'ko', repr(bad))
        c.send('team1\n')
        self.assertEqual(c.readline(), '4')
        self.assertEqual(c.readline(), '10 10')

    def test_team_and_commands_in_one_packet(self):
        srv = self.server()
        c = self.raw(srv)
        c.readline()
        c.send('team1\nvoir\ninventaire\n')
        self.assertEqual(c.readline(), '4')
        self.assertEqual(c.readline(), '10 10')
        self.assertTrue(c.readline().startswith('{'))
        self.assertRegex(c.readline(), INVENTORY_RE)

    def test_handshake_byte_by_byte(self):
        srv = self.server()
        c = self.raw(srv)
        c.readline()
        for ch in 'team1\navance\n':
            c.send(ch)
            time.sleep(0.005)
        self.assertEqual([c.readline(), c.readline(), c.readline()], ['4', '10 10', 'ok'])

    def test_crlf_line_endings(self):
        srv = self.server()
        c = self.raw(srv)
        c.readline()
        c.send('team1\r\navance\r\n')
        self.assertEqual([c.readline(), c.readline(), c.readline()], ['4', '10 10', 'ok'])

    def test_slot_not_given_back_after_disconnect(self):
        srv = self.server(c=1)
        p = Player(srv.port)
        self.assertEqual(p.join('team1')[1], '0')
        p.close()
        time.sleep(0.2)
        q = Player(srv.port)
        self._conns.append(q)
        self.assertEqual(q.join('team1')[1], 'ko')

    def test_slot_not_given_back_after_death(self):
        srv = self.server(c=1, t=1000)
        p = Player(srv.port)
        self._conns.append(p)
        p.join('team1')
        self.assertEqual(p.readline(3), 'mort')
        q = Player(srv.port)
        self._conns.append(q)
        self.assertEqual(q.join('team1')[1], 'ko')

    def test_graphic_client_does_not_use_a_slot(self):
        srv = self.server(c=1)
        g = self.gfx(srv)
        g2 = self.gfx(srv)
        p = self.player(srv, g)
        self.assertIsNotNone(p.id)
        self.assertTrue(g2.wait_for(lambda: p.id in g2.players))


# =============================================================================
# III.4 - Commands: syntax, answers and delays
# =============================================================================
class T05_Commands(ZappyCase):
    def test_every_command_delay(self):
        t = 100
        srv = self.server(t=t)
        g = self.gfx(srv)
        p = self.player(srv, g)
        other = self.player(srv, g)
        other.goto(p.x, p.y)
        for command, units, answers in [
            ('avance', 7, {'ok'}), ('droite', 7, {'ok'}), ('gauche', 7, {'ok'}),
            ('voir', 7, None), ('inventaire', 1, None), ('prend thystame', 7, {'ok', 'ko'}),
            ('pose linemate', 7, {'ko'}), ('broadcast hi', 7, {'ok'}), ('fork', 42, {'ok'}),
            ('connect_nbr', 0, None),
        ]:
            with self.subTest(command=command):
                answer, elapsed = p.timed(command)
                if answers:
                    self.assertIn(answer, answers)
                self.assertDuration(elapsed, units, t, command)
        self.sync(g, p)
        other.goto(p.x, p.y)
        answer, elapsed = p.timed('expulse')
        self.assertEqual(answer, 'ok')
        self.assertDuration(elapsed, 7, t, 'expulse')

    def test_requests_are_queued_and_executed_in_order(self):
        srv = self.server(t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        start = time.time()
        p.send('droite\ninventaire\nvoir\ngauche\n')
        answers = [p.readline() for _ in range(4)]
        elapsed = time.time() - start
        self.assertEqual(answers[0], 'ok')
        self.assertRegex(answers[1], INVENTORY_RE)
        self.assertTrue(answers[2].startswith('{') and not INVENTORY_RE.match(answers[2]))
        self.assertEqual(answers[3], 'ok')
        self.assertDuration(elapsed, 7 + 1 + 7 + 7, 100, 'four chained requests')

    def test_at_most_ten_pending_requests(self):
        srv = self.server(t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.send('avance\n' * 15)
        answers = p.lines_for(15 * 0.07 + 0.5)
        self.assertEqual(answers, ['ok'] * 10)
        # the queue is usable again afterwards
        self.assertEqual(p.cmd('droite'), 'ok')

    def test_a_long_action_only_blocks_its_player(self):
        srv = self.server(t=100)
        g = self.gfx(srv)
        a, b = self.player(srv, g), self.player(srv, g)
        a.send('fork\n')
        answer, elapsed = b.timed('avance')
        self.assertEqual(answer, 'ok')
        self.assertLess(elapsed, 0.2)
        self.assertEqual(a.readline(), 'ok')

    def test_strict_syntax(self):
        srv = self.server()
        g = self.gfx(srv)
        p = self.player(srv, g)
        for bad in ('', 'Avance', 'AVANCE', 'avance ', ' avance', 'avancer', 'voir now', 'prend', 'prend ',
                    'prend gold', 'prend  linemate', 'pose', 'pose gold', 'broadcast', 'forward', 'Right',
                    'incantation now', 'connect_nbr 3', '\x01\x02'):
            with self.subTest(command=repr(bad)):
                self.assertEqual(p.cmd(bad), 'ko')

    def test_avance_droite_gauche_and_round_world(self):
        srv = self.server(x=7, y=5)
        g = self.gfx(srv)
        p = self.player(srv, g)

        def gfx_state():
            g.wait_for(lambda: (g.players[p.id]['x'], g.players[p.id]['y'], g.players[p.id]['dir'])
                       == (p.x, p.y, p.dir), 2)
            q = g.players[p.id]
            return q['x'], q['y'], q['dir']

        p.goto(0, 0)
        for direction, expected in ((1, (0, 4)), (4, (6, 4)), (3, (6, 0)), (2, (0, 0))):
            p.turn_to(direction)
            p.advance()
            with self.subTest(direction=direction):
                self.assertEqual(gfx_state(), (*expected, direction))
        p.turn_to(1)
        self.assertEqual(p.cmd('droite'), 'ok')
        p.dir = 2
        self.assertEqual(gfx_state()[2], 2)
        self.assertEqual(p.cmd('gauche'), 'ok')
        self.assertEqual(p.cmd('gauche'), 'ok')
        p.dir = 4
        self.assertEqual(gfx_state()[2], 4)
        for _ in range(4):
            p.cmd('droite')
        self.assertEqual(gfx_state()[2], 4)

    def test_inventory_format_and_initial_content(self):
        srv = self.server()
        g = self.gfx(srv)
        p = self.player(srv, g)
        line = p.cmd('inventaire')
        self.assertRegex(line, INVENTORY_RE)
        inv = p.inventory()
        self.assertEqual(inv['nourriture'], 10)
        self.assertTrue(all(inv[s] == 0 for s in STONES))

    def test_prend_and_pose(self):
        srv = self.server(x=6, y=6, t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        for stone in ('linemate', 'sibur'):
            with self.subTest(stone=stone):
                pos = g.find_tile(lambda t: t[stone] > 0, near=(p.x, p.y))
                p.goto(*pos)
                before = g.tiles[pos][stone]
                mark = g.mark()
                self.assertEqual(p.cmd(f'prend {stone}'), 'ok')
                self.assertEqual(p.inventory()[stone], 1)
                self.assertTrue(g.wait_line('bct', mark, match=lambda l: l.split()[1:3] == [str(pos[0]), str(pos[1])]))
                self.assertEqual(g.tiles[pos][stone], before - 1)
                self.assertTrue(g.wait_line('pin', mark, match=lambda l: l.split()[1] == str(p.id)))
                self.assertEqual(p.cmd(f'pose {stone}'), 'ok')
                self.assertEqual(p.inventory()[stone], 0)
                self.assertEqual(p.cmd(f'pose {stone}'), 'ko')
        empty = g.find_tile(lambda t: t['thystame'] == 0, near=(p.x, p.y))
        p.goto(*empty)
        self.assertEqual(p.cmd('prend thystame'), 'ko')

    def test_prend_nourriture_adds_food(self):
        srv = self.server(x=6, y=6, t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.goto(*g.find_tile(lambda t: t['nourriture'] > 0, near=(p.x, p.y)))
        before = p.inventory()['nourriture']
        self.assertEqual(p.cmd('prend nourriture'), 'ok')
        self.assertEqual(p.inventory()['nourriture'], before + 1)
        self.assertEqual(p.cmd('pose nourriture'), 'ok')
        self.assertEqual(p.inventory()['nourriture'], before)

    def test_connect_nbr(self):
        srv = self.server(c=4)
        g = self.gfx(srv)
        p = self.player(srv, g)
        self.assertEqual(p.cmd('connect_nbr'), '3')
        self.player(srv, g)
        self.assertEqual(p.cmd('connect_nbr'), '2')
        other = self.player(srv, g, 'team2')
        self.assertEqual(p.cmd('connect_nbr'), '2')
        self.assertEqual(other.cmd('connect_nbr'), '3')


# =============================================================================
# II.6 - Field of view
# =============================================================================
class T06_Vision(ZappyCase):
    def expected(self, g, p, level):
        squares = vision_squares(p.x, p.y, p.dir, level, g.w, g.h)
        out = []
        with g.lock:
            for sq in squares:
                c = Counter({r: n for r, n in g.tiles[sq].items() if n})
                c['player'] = sum(1 for pid, q in g.players.items() if pid != p.id and (q['x'], q['y']) == sq)
                out.append(+c)
        return out

    def test_level1_every_orientation_with_round_world(self):
        srv = self.server(x=5, y=5, t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        other = self.player(srv, g)
        p.goto(0, 0)
        other.goto(4, 4)
        for direction in (1, 2, 3, 4):
            with self.subTest(direction=direction):
                p.turn_to(direction)
                seen = parse_vision(p.cmd('voir'))
                self.assertEqual(len(seen), 4)
                self.assertEqual(seen, self.expected(g, p, 1))

    def test_other_player_seen_but_not_self(self):
        srv = self.server(x=9, y=9)
        g = self.gfx(srv)
        p, q = self.player(srv, g), self.player(srv, g)
        p.goto(4, 4)
        p.turn_to(1)
        q.goto(4, 3)
        seen = parse_vision(p.cmd('voir'))
        self.assertEqual(seen[0]['player'], 0)
        self.assertEqual(seen[2]['player'], 1)
        q.goto(4, 4)
        self.assertEqual(parse_vision(p.cmd('voir'))[0]['player'], 1)

    def test_format_matches_subject_example(self):
        srv = self.server(x=9, y=9)
        g = self.gfx(srv)
        p = self.player(srv, g)
        line = p.cmd('voir')
        self.assertRegex(line, r'^\{[a-z ]*(, [a-z ]*){3}\}$')
        for tile in line[1:-1].split(', '):
            self.assertNotRegex(tile, r'^ | $|  ')

    def test_level2_sees_nine_squares(self):
        srv = self.server(x=5, y=5, t=200)
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.goto(*g.find_tile(lambda t: t['linemate'] > 0, near=(p.x, p.y)))
        self.assertEqual(p.cmd('incantation'), 'elevation en cours')
        self.assertEqual(p.readline(3), 'niveau actuel : 2')
        g.wait_for(lambda: g.players[p.id]['level'] == 2)
        time.sleep(0.2)
        for direction in (1, 2, 3, 4):
            with self.subTest(direction=direction):
                p.turn_to(direction)
                seen = parse_vision(p.cmd('voir'))
                self.assertEqual(len(seen), 9)
                self.assertEqual(seen, self.expected(g, p, 2))


# =============================================================================
# II.7 / III.10 - Sound transmission (broadcast)
# =============================================================================
class T07_Broadcast(ZappyCase):
    def test_message_format_and_recipients(self):
        srv = self.server(x=9, y=9)
        g = self.gfx(srv)
        a, b = self.player(srv, g), self.player(srv, g, 'team2')
        mark = g.mark()
        self.assertEqual(a.cmd('broadcast hello world, 42 !'), 'ok')
        line = b.readline()
        self.assertRegex(line, r'^message [0-8],hello world, 42 !$')
        self.assertEqual(a.lines_for(0.2), [], 'the emitter must not hear itself')
        self.assertTrue(g.wait_line('pbc', mark))
        self.assertEqual([l for l in g.lines(since=mark) if l.startswith('message')], [])

    def test_same_square_is_zero(self):
        srv = self.server(x=1, y=1)
        g = self.gfx(srv)
        a, b = self.player(srv, g), self.player(srv, g)
        a.cmd('broadcast x')
        self.assertEqual(b.readline(), 'message 0,x')

    def check_orientation(self, rdir):
        w = h = 11
        srv = self.server(x=w, y=h, t=200)
        g = self.gfx(srv)
        r, e = self.player(srv, g), self.player(srv, g)
        r.goto(5, 5)
        r.turn_to(rdir)
        cases = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)] + [(2, 0), (0, -2), (-2, 2), (2, 2)]
        for dx, dy in cases:
            e.goto(5 + dx, 5 + dy)
            e.cmd('broadcast ping')
            line = r.readline()
            want = expected_sound_k(r.x, r.y, r.dir, e.x, e.y, w, h)
            with self.subTest(receiver_dir=rdir, emitter=(dx, dy)):
                self.assertEqual(line, f'message {want},ping')

    def test_directions_facing_north(self):
        self.check_orientation(1)

    def test_directions_facing_east(self):
        self.check_orientation(2)

    def test_directions_facing_south(self):
        self.check_orientation(3)

    def test_directions_facing_west(self):
        self.check_orientation(4)

    def test_shortest_path_through_the_edge(self):
        w = h = 11
        srv = self.server(x=w, y=h, t=200)
        g = self.gfx(srv)
        r, e = self.player(srv, g), self.player(srv, g)
        r.goto(0, 5)
        r.turn_to(1)
        for ex, ey in ((9, 5), (10, 4), (0, 9), (2, 5), (9, 3)):
            e.goto(ex, ey)
            e.cmd('broadcast wrap')
            want = expected_sound_k(r.x, r.y, r.dir, ex, ey, w, h)
            with self.subTest(emitter=(ex, ey)):
                self.assertEqual(r.readline(), f'message {want},wrap')


# =============================================================================
# III.11 - Kick
# =============================================================================
class T08_Expulse(ZappyCase):
    def test_alone_is_ko(self):
        srv = self.server(x=30, y=30)
        g = self.gfx(srv)
        p = self.player(srv, g)
        with g.lock:
            alone = all((q['x'], q['y']) != (p.x, p.y) for pid, q in g.players.items() if pid != p.id)
        self.assertTrue(alone)
        self.assertEqual(p.cmd('expulse'), 'ko')

    def test_push_direction_and_deplacement(self):
        w = h = 11
        srv = self.server(x=w, y=h, t=100)
        g = self.gfx(srv)
        pusher, victim, witness = self.player(srv, g), self.player(srv, g, 'team2'), self.player(srv, g)
        witness.goto(0, 0)
        for push_dir in (1, 2, 3, 4):
            for victim_dir in (1, 3):
                with self.subTest(push_dir=push_dir, victim_dir=victim_dir):
                    pusher.goto(5, 5)
                    victim.goto(5, 5)
                    victim.turn_to(victim_dir)
                    pusher.turn_to(push_dir)
                    mark = g.mark()
                    self.assertEqual(pusher.cmd('expulse'), 'ok')
                    dx, dy = DIRS[push_dir]
                    new = ((5 + dx) % w, (5 + dy) % h)
                    want = expected_sound_k(new[0], new[1], victim_dir, 5, 5, w, h)
                    self.assertEqual(victim.readline(), f'deplacement {want}')
                    self.assertTrue(g.wait_line('ppo', mark, match=lambda l: l.split()[1:4] == [str(victim.id), str(new[0]), str(new[1])]))
                    self.assertTrue(g.wait_line('pex', mark))
                    victim.x, victim.y = new
        self.assertEqual(witness.lines_for(0.1), [])
        self.assertEqual(g.players[witness.id]['x'], 0)

    def test_pushes_everybody_on_the_square(self):
        srv = self.server(x=1, y=1)
        g = self.gfx(srv)
        a, b, c = self.player(srv, g), self.player(srv, g), self.player(srv, g, 'team2')
        self.assertEqual(a.cmd('expulse'), 'ok')
        self.assertTrue(b.readline().startswith('deplacement '))
        self.assertTrue(c.readline().startswith('deplacement '))


# =============================================================================
# II.4 / III.3 - Food and death
# =============================================================================
class T09_LifeAndDeath(ZappyCase):
    def time_to_death(self, srv, p):
        start = time.time()
        line = p.readline(5)
        self.assertEqual(line, 'mort')
        elapsed = time.time() - start
        self.assertTrue(p.wait_closed(2), 'socket not closed after mort')
        return elapsed

    def test_ten_food_units_last_1260_time_units(self):
        srv = self.server(t=1000, x=30, y=30)
        g = self.gfx(srv)
        p = self.player(srv, g)
        mark = g.mark()
        elapsed = self.time_to_death(srv, p)
        self.assertGreater(elapsed, 1.15)
        self.assertLess(elapsed, 1.36)
        self.assertTrue(g.wait_line('pdi', mark, match=lambda l: l.split()[1] == str(p.id)))

    def test_food_counter_decreases_every_126_units(self):
        srv = self.server(t=100, x=30, y=30)
        g = self.gfx(srv)
        p = self.player(srv, g)
        self.assertEqual(p.inventory()['nourriture'], 10)
        time.sleep(1.30)
        self.assertEqual(p.inventory()['nourriture'], 9)
        time.sleep(1.26)
        self.assertEqual(p.inventory()['nourriture'], 8)

    def test_eating_extends_life(self):
        srv = self.server(t=1000, x=1, y=1)
        g = self.gfx(srv)
        p = self.player(srv, g)
        self.assertEqual(p.cmd('prend nourriture'), 'ok')
        elapsed = self.time_to_death(srv, p)
        self.assertGreater(elapsed, 1.30)
        self.assertLess(elapsed, 1.50)

    def test_dropping_food_shortens_life(self):
        srv = self.server(t=1000, x=30, y=30)
        g = self.gfx(srv)
        p = self.player(srv, g)
        self.assertEqual(p.cmd('pose nourriture'), 'ok')
        elapsed = self.time_to_death(srv, p)
        self.assertGreater(elapsed, 1.05)
        self.assertLess(elapsed, 1.23)

    def test_dead_player_disappears_from_vision(self):
        srv = self.server(t=1000, x=1, y=1, c=5)
        g = self.gfx(srv)
        a = self.player(srv, g)
        time.sleep(0.4)
        b = self.player(srv, g)
        self.assertEqual(a.readline(3), 'mort')
        self.assertEqual(parse_vision(b.cmd('voir'))[0]['player'], 0)


# =============================================================================
# III.8 - Player reproduction
# =============================================================================
class T10_Reproduction(ZappyCase):
    def test_fork_egg_hatch_and_connection(self):
        t = 1000
        srv = self.server(t=t, c=1, x=8, y=8)
        g = self.gfx(srv)
        p = self.player(srv, g)
        self.assertEqual(p.cmd('connect_nbr'), '0')
        mark = g.mark()
        answer, elapsed = p.timed('fork')
        self.assertEqual(answer, 'ok')
        self.assertDuration(elapsed, 42, t, 'fork')
        laid = time.time()
        enw = g.wait_line('enw', mark)
        self.assertIsNotNone(enw)
        egg_id, parent, ex, ey = map(int, enw.split()[1:5])
        self.assertEqual((parent, ex, ey), (p.id, p.x, p.y))
        # the parent can move while the egg incubates
        self.assertEqual(p.cmd('avance'), 'ok')
        refused = Player(srv.port)
        self._conns.append(refused)
        self.assertEqual(refused.join('team1')[1], 'ko')
        self.assertTrue(g.wait_line('eht', mark, timeout=2))
        hatch = time.time() - laid
        self.assertGreater(hatch, 0.6 * 0.85)
        self.assertLess(hatch, 0.6 + 0.1)
        self.assertEqual(p.cmd('connect_nbr'), '1')
        child = self.player(srv, g)
        self.assertEqual((child.x, child.y), (ex, ey))
        self.assertIn(child.dir, (1, 2, 3, 4))
        self.assertTrue(g.wait_line('ebo', mark, match=lambda l: l.split()[1] == str(egg_id)))
        self.assertEqual(p.cmd('connect_nbr'), '0')

    def test_eggs_belong_to_the_team_of_the_parent(self):
        srv = self.server(t=1000, c=1)
        g = self.gfx(srv)
        p = self.player(srv, g, 'team1')
        q = self.player(srv, g, 'team2')
        p.cmd('fork')
        time.sleep(0.8)
        self.assertEqual(p.cmd('connect_nbr'), '1')
        self.assertEqual(q.cmd('connect_nbr'), '0')

    def test_orientation_of_new_players_is_random(self):
        srv = self.server(c=40, x=30, y=30)
        g = self.gfx(srv)
        dirs = {self.player(srv, g).dir for _ in range(24)}
        self.assertGreaterEqual(len(dirs), 3)


# =============================================================================
# II.5 - Elevation ritual
# =============================================================================
class T11_Incantation(ZappyCase):
    def test_level1_success_timing_and_stone_consumed(self):
        t = 200
        srv = self.server(x=6, y=6, t=t)
        g = self.gfx(srv)
        p = self.player(srv, g)
        pos = g.find_tile(lambda tile: tile['linemate'] > 0, near=(p.x, p.y))
        p.goto(*pos)
        before = dict(g.tiles[pos])
        mark = g.mark()
        answer, elapsed = p.timed('incantation')
        self.assertEqual(answer, 'elevation en cours')
        self.assertLess(elapsed, 0.05)
        start = time.time()
        self.assertEqual(p.readline(3), 'niveau actuel : 2')
        self.assertDuration(time.time() - start + elapsed, 300, t, 'incantation')
        self.assertTrue(g.wait_line('pic', mark, match=lambda l: l.split()[1:4] == [str(pos[0]), str(pos[1]), '1']))
        self.assertTrue(g.wait_line('pie', mark, match=lambda l: l.split()[3] == '1'))
        self.assertTrue(g.wait_line('plv', mark, match=lambda l: l.split()[1:] == [str(p.id), '2']))
        consumed = [l for l in g.lines('bct', mark) if l.split()[1:3] == [str(pos[0]), str(pos[1])]]
        self.assertEqual(int(consumed[0].split()[4]), before['linemate'] - 1)

    def test_ko_without_stones(self):
        srv = self.server(x=6, y=6)
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.goto(*g.find_tile(lambda t: t['linemate'] == 0, near=(p.x, p.y)))
        self.assertEqual(p.cmd('incantation'), 'ko')

    def test_level2_needs_two_players(self):
        srv = self.server(x=1, y=1, t=200)
        g = self.gfx(srv)
        p = self.player(srv, g)
        self.assertEqual(p.cmd('incantation'), 'elevation en cours')
        self.assertEqual(p.readline(3), 'niveau actuel : 2')
        g.wait_for(lambda: all(g.tiles[(0, 0)][s] >= 1 for s in ('linemate', 'deraumere', 'sibur')), 2)
        self.assertEqual(p.cmd('incantation'), 'ko')

    def test_group_levels_frozen_players_and_other_levels(self):
        t = 200
        srv = self.server(x=1, y=1, t=t)
        g = self.gfx(srv)
        a, b = self.player(srv, g), self.player(srv, g, 'team2')
        # level 1 -> 2: every player of the same level on the square takes part
        self.assertEqual(a.cmd('incantation'), 'elevation en cours')
        self.assertEqual(b.readline(), 'elevation en cours')
        self.assertEqual(a.readline(3), 'niveau actuel : 2')
        self.assertEqual(b.readline(3), 'niveau actuel : 2')
        c = self.player(srv, g)
        g.wait_for(lambda: all(g.tiles[(0, 0)][s] >= 1 for s in ('linemate', 'deraumere', 'sibur')), 2)
        # level 2 -> 3 with players of different teams; c (level 1) does not count
        self.assertEqual(a.cmd('incantation'), 'elevation en cours')
        self.assertEqual(b.readline(), 'elevation en cours')
        # b is frozen: its request waits for the end of the ritual
        b.send('inventaire\n')
        self.assertEqual(b.readline(3), 'niveau actuel : 3')
        self.assertRegex(b.readline(), INVENTORY_RE)
        self.assertEqual(a.readline(3), 'niveau actuel : 3')
        self.assertEqual(c.lines_for(0.2), [])
        self.assertEqual(g.players[c.id]['level'], 1)
        self.assertEqual(len(parse_vision(a.cmd('voir'))), 16)

    def test_participant_leaving_does_not_break_the_server(self):
        srv = self.server(x=1, y=1, t=200)
        g = self.gfx(srv)
        a, b = self.player(srv, g), self.player(srv, g)
        self.assertEqual(a.cmd('incantation'), 'elevation en cours')
        b.close()
        self.assertEqual(a.readline(3), 'niveau actuel : 2')
        self.assertTrue(srv.alive())


# =============================================================================
# II.2 - Resources
# =============================================================================
class T12_Resources(ZappyCase):
    def test_every_resource_is_generated(self):
        srv = self.server(x=20, y=20)
        g = self.gfx(srv)
        tot = g.totals()
        for r in ('nourriture', *STONES):
            self.assertGreater(tot[r], 0, r)
        self.assertGreater(tot['nourriture'], tot['thystame'])
        self.assertGreater(tot['linemate'], tot['thystame'])

    def test_generation_is_random(self):
        maps = []
        for _ in range(2):
            srv = self.server(x=15, y=15)
            g = self.gfx(srv)
            maps.append(dict(g.tiles))
        self.assertNotEqual(maps[0], maps[1])

    def test_resources_regenerate_without_overflowing(self):
        srv = self.server(x=6, y=6, t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        initial = g.totals()
        pos = g.find_tile(lambda t: t['linemate'] > 0, near=(p.x, p.y))
        p.goto(*pos)
        self.assertEqual(p.cmd('prend linemate'), 'ok')
        self.assertTrue(g.wait_for(lambda: g.totals()['linemate'] >= initial['linemate'], 1.0),
                        'the taken linemate was not regenerated')
        time.sleep(1.0)
        after = g.totals()
        for r in initial:
            self.assertLessEqual(after[r], initial[r] + 1, f'{r} keeps growing')


# =============================================================================
# III.6 - Time: no active wait, no blocking
# =============================================================================
class T13_NoActiveWaitNoBlocking(ZappyCase):
    def test_idle_server_sleeps(self):
        srv = self.server(t=100)
        g = self.gfx(srv)
        players = [self.player(srv, g) for _ in range(3)]
        c0 = cpu_seconds(srv.pid)
        time.sleep(2)
        self.assertLess(cpu_seconds(srv.pid) - c0, 0.1)
        self.assertEqual(len(players), 3)

    def test_server_sleeps_during_a_long_action(self):
        srv = self.server(t=1)
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.send('avance\navance\nvoir\n')
        c0 = cpu_seconds(srv.pid)
        time.sleep(2)
        self.assertLess(cpu_seconds(srv.pid) - c0, 0.05)

    def test_server_sleeps_while_frozen_players_have_requests(self):
        srv = self.server(t=10, x=1, y=1)
        g = self.gfx(srv)
        a, b = self.player(srv, g), self.player(srv, g)
        self.assertEqual(a.cmd('incantation', timeout=3), 'elevation en cours')
        b.send('avance\nvoir\n')
        c0 = cpu_seconds(srv.pid)
        time.sleep(2)
        self.assertLess(cpu_seconds(srv.pid) - c0, 0.05)

    def test_stopped_graphic_client_does_not_block(self):
        srv = self.server(x=500, y=500, t=100)
        g = self.gfx(srv)
        p = self.player(srv, g)
        frozen = socket.socket()
        frozen.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        frozen.connect(('127.0.0.1', srv.port))
        self._conns.append(Conn.__new__(Conn))
        self._conns[-1].sock = frozen
        frozen.sendall(b'GRAPHIC\n')     # ~6 MB of map that it never reads
        time.sleep(0.5)
        answer, elapsed = p.timed('avance')
        self.assertEqual(answer, 'ok')
        self.assertLess(elapsed, 0.3)
        c = self.raw(srv)
        self.assertEqual(c.readline(1), 'BIENVENUE')

    def test_stopped_player_does_not_block(self):
        srv = self.server(x=30, y=30, t=1000, c=10)
        g = self.gfx(srv)
        lazy, active = self.player(srv, g), self.player(srv, g)
        lazy.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        for _ in range(50):
            active.cmd('broadcast ' + 'x' * 4000)
        answer, elapsed = active.timed('avance')
        self.assertEqual(answer, 'ok')
        self.assertLess(elapsed, 0.2)

    def test_unread_stdout_pipe_does_not_block(self):
        port = __import__('zappy_testlib').free_port()
        proc = subprocess.Popen([SERVER, '-p', str(port), '-x', '5', '-y', '5', '-n', 'team1', '-c', '5',
                                 '-t', '100'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.3)
            p = Player(port)
            self._conns.append(p)
            p.join('team1')
            for _ in range(300):                  # 3000 commands, the pipe is never read
                p.send('connect_nbr\n' * 10)
                answers = [p.readline(1) for _ in range(10)]
                self.assertEqual(answers, ['4'] * 10)
            self.assertEqual(p.cmd('avance'), 'ok')
        finally:
            proc.kill()
            proc.wait()

    def test_stopped_client_process_does_not_block(self):
        srv = self.server(t=100)
        g = self.gfx(srv)
        proc = subprocess.Popen([sys.executable, CLIENT, '-n', 'team1', '-p', str(srv.port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            g.wait_for(lambda: len(g.players) == 1, 3)
            proc.send_signal(signal.SIGSTOP)
            p = self.player(srv, g)
            answer, elapsed = p.timed('voir')
            self.assertTrue(answer.startswith('{'))
            self.assertLess(elapsed, 0.2)
        finally:
            proc.send_signal(signal.SIGCONT)
            proc.kill()
            proc.wait()


# =============================================================================
# Chapter V - "In no way can your program quit in an unexpected manner"
# =============================================================================
class T14_Robustness(ZappyCase):
    def test_binary_garbage(self):
        srv = self.server()
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.send(bytes(range(256)) + b'\n' + os.urandom(3000).replace(b'\n', b'') + b'\n')
        answers = p.lines_for(0.5)
        self.assertTrue(all(a == 'ko' for a in answers))
        self.assertEqual(p.cmd('avance'), 'ok')

    def test_huge_line_without_newline(self):
        srv = self.server()
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.send('a' * 2_000_000)
        p.send('\navance\n')
        self.assertIn('ok', p.lines_for(1.5))
        self.assertTrue(srv.alive())

    def test_garbage_before_handshake(self):
        srv = self.server()
        c = self.raw(srv)
        c.readline()
        c.send(os.urandom(5000).replace(b'\n', b'x') + b'\nteam1\n')
        self.assertEqual(c.lines_for(0.5)[-2:], ['4', '10 10'])

    def test_many_connections(self):
        srv = self.server(c=300)
        conns = [Conn(srv.port) for _ in range(300)]
        self._conns += conns
        self.assertTrue(all(c.readline(3) == 'BIENVENUE' for c in conns))
        for c in conns[:150]:
            c.send('team1\n')
        self.assertTrue(all(c.readline(3) is not None for c in conns[:150]))
        self.assertTrue(srv.alive())

    def test_connect_disconnect_storm(self):
        srv = self.server()
        for i in range(200):
            s = socket.create_connection(('127.0.0.1', srv.port))
            if i % 2:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))   # RST
            else:
                s.sendall(b'team1\nvoir\n')
            s.close()
        c = self.raw(srv)
        self.assertEqual(c.readline(), 'BIENVENUE')
        self.assertTrue(srv.alive())

    def test_disconnect_with_pending_requests(self):
        srv = self.server(t=10)
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.send('avance\n' * 10 + 'fork\n')
        p.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        p.close()
        q = self.player(srv, g)
        self.assertEqual(q.cmd('droite'), 'ok')

    def test_half_closed_client(self):
        srv = self.server()
        g = self.gfx(srv)
        p = self.player(srv, g)
        p.send('voir\n')
        p.sock.shutdown(socket.SHUT_WR)
        time.sleep(0.3)
        self.assertTrue(srv.alive())
        self.assertEqual(self.player(srv, g).cmd('avance'), 'ok')

    def test_graphic_client_cannot_play(self):
        """Player commands sent by a monitor are answered with suc, never executed."""
        srv = self.server()
        g = self.gfx(srv)
        g.send('avance\nvoir\nmsz\nprend linemate\nGRAPHIC\n')
        p = self.player(srv, g)
        self.assertEqual(p.cmd('avance'), 'ok')
        self.assertEqual([l for l in g.lines() if l in ('ok', 'ko')], [])

    def test_server_survives_a_full_game_with_disconnections(self):
        srv = self.server(t=1000, x=5, y=5, c=20)
        g = self.gfx(srv)
        players = [self.player(srv, g, ('team1', 'team2')[i % 2]) for i in range(10)]
        for i, p in enumerate(players):
            p.send('avance\nvoir\nbroadcast yo\nexpulse\nfork\nincantation\nprend nourriture\npose linemate\n')
            if i % 3 == 0:
                p.close()
        time.sleep(2)
        self.assertTrue(srv.alive())
        self.assertEqual(Conn(srv.port).readline(), 'BIENVENUE')


# =============================================================================
# III.12 - Data sent to the graphic client
# =============================================================================
class T15_GraphicProtocol(ZappyCase):
    def test_initial_state(self):
        srv = self.server(x=7, y=4, t=42, teams=('red', 'blue'))
        first = self.gfx(srv)
        p = self.player(srv, first, 'blue')
        p.cmd('fork')
        g = self.gfx(srv)
        self.assertEqual((g.w, g.h, g.t, g.teams), (7, 4, 42, ['red', 'blue']))
        self.assertEqual(set(g.tiles), {(x, y) for x in range(7) for y in range(4)})
        self.assertEqual(g.players[p.id]['team'], 'blue')
        self.assertTrue(g.wait_for(lambda: g.players[p.id]['inv'] is not None))
        self.assertEqual(len(g.eggs), 1)

    def test_events(self):
        srv = self.server(x=1, y=1, t=500)
        g = self.gfx(srv)
        mark = g.mark()
        a = self.player(srv, g)
        b = self.player(srv, g)
        expectations = {
            'avance': 'ppo', 'droite': 'ppo', 'gauche': 'ppo', 'prend nourriture': 'pin',
            'broadcast hey': 'pbc', 'expulse': 'pex', 'fork': 'enw', 'incantation': 'pic',
        }
        for command, event in expectations.items():
            with self.subTest(command=command):
                m = g.mark()
                a.send(command + '\n')
                self.assertIsNotNone(g.wait_line(event, m, match=lambda l: str(a.id) in l.split()[1:] or event in ('pic',)))
        for event in ('pnw', 'pin', 'bct', 'pie', 'plv', 'eht'):
            with self.subTest(event=event):
                self.assertIsNotNone(g.wait_line(event, mark, timeout=3))
        b.close()
        self.assertIsNotNone(g.wait_line('pdi', mark, match=lambda l: l.split()[1] == str(b.id)))
        self.assertEqual(g.wait_line('pbc', mark), f'pbc {a.id} hey')

    def test_several_graphic_clients(self):
        srv = self.server()
        g1, g2 = self.gfx(srv), self.gfx(srv)
        p = self.player(srv, g1)
        p.advance()
        for g in (g1, g2):
            self.assertTrue(g.wait_for(lambda g=g: p.id in g.players and (g.players[p.id]['x'], g.players[p.id]['y']) == (p.x, p.y)))


# =============================================================================
# protocol-gfx.pdf - Server / graphical monitor protocol, to the letter
# =============================================================================
ID = r'#\d+'
NUM = r'\d+'
Q7 = r'( \d+){7}'


class T15b_GraphicProtocolRFC(ZappyCase):
    def monitor(self, srv):
        """Raw monitor: returns the connection and its whole initial sequence."""
        m = self.raw(srv)
        self.assertEqual(m.readline(), 'BIENVENUE')
        m.send('GRAPHIC\n')
        return m, m.lines_for(0.4)

    def ask(self, m, request, accept):
        """Sends a monitor request and returns the first line starting with one
        of the `accept` prefixes (asynchronous events are skipped)."""
        m.send(request + '\n')
        end = time.time() + 3
        while time.time() < end:
            line = m.readline(max(0.01, end - time.time()))
            if line is not None and line.split(' ', 1)[0] in accept:
                return line
        return None

    def assertFormat(self, line, pattern):
        self.assertIsNotNone(line)
        self.assertRegex(line, '^' + pattern + '$')

    def test_initial_sequence_order(self):
        srv = self.server(x=3, y=2, t=100, teams=('red', 'blue'))
        g = self.gfx(srv)
        p = self.player(srv, g, 'red')
        self.assertEqual(p.cmd('fork'), 'ok')
        _, lines = self.monitor(srv)
        expected = ['msz', 'sgt'] + ['bct'] * 6 + ['tna'] * 2 + ['pnw', 'enw']
        self.assertEqual([l.split(' ', 1)[0] for l in lines[:len(expected)]], expected)
        self.assertEqual(lines[0], 'msz 3 2')
        self.assertEqual(lines[1], 'sgt 100')
        self.assertEqual([l.split()[1:3] for l in lines[2:8]], [[str(x), str(y)] for y in range(2) for x in range(3)])
        self.assertEqual(lines[8:10], ['tna red', 'tna blue'])
        self.assertFormat(lines[10], f'pnw #{p.id} {NUM} {NUM} [1-4] 1 red')
        self.assertFormat(lines[11], f'enw {ID} #{p.id} {p.x} {p.y}')

    def test_numbers_are_written_with_a_sharp(self):
        srv = self.server(x=1, y=1, t=500)
        g = self.gfx(srv)
        a = self.player(srv, g)
        b = self.player(srv, g)
        for c in ('avance', 'broadcast hello world', 'pose nourriture', 'expulse', 'fork', 'incantation'):
            a.send(c + '\n')
        patterns = {
            'pnw': f'pnw {ID} 0 0 [1-4] {NUM} team1',
            'ppo': f'ppo {ID} 0 0 [1-4]',
            'pbc': f'pbc #{a.id} hello world',
            'pdr': f'pdr #{a.id} 0',
            'pin': f'pin {ID} 0 0{Q7}',
            'bct': f'bct 0 0{Q7}',
            'pex': f'pex #{a.id}',
            'pfk': f'pfk #{a.id}',
            'enw': f'enw {ID} #{a.id} 0 0',
            'pic': f'pic 0 0 1( {ID})+',
            'pie': 'pie 0 0 [01]',
            'plv': f'plv {ID} [12]',
        }
        self.assertTrue(g.wait_line('pie', 0, timeout=3))
        b.close()
        self.assertTrue(g.wait_line('pdi', 0))
        patterns['pdi'] = f'pdi #{b.id}'
        with g.lock:
            raw = list(g.raw)
        for prefix, pattern in patterns.items():
            with self.subTest(event=prefix):
                lines = [l for l in raw if l.split(' ', 1)[0] == prefix]
                self.assertTrue(lines, f'no {prefix}')
                for line in lines:
                    self.assertFormat(line, pattern)

    def test_event_order(self):
        srv = self.server(x=1, y=1, t=500)
        g = self.gfx(srv)
        a = self.player(srv, g)
        b = self.player(srv, g)

        def sequence(command, prefixes):
            mark = g.mark()
            a.send(command + '\n')
            self.assertTrue(g.wait_line(prefixes[-1], mark, timeout=3), f'{command}: no {prefixes[-1]}')
            time.sleep(0.05)
            seq = [l for l in g.lines(since=mark) if l.split(' ', 1)[0] in prefixes]
            # Hunger 'pin' updates may come first: the sequence starts with its own event
            first = next(i for i, l in enumerate(seq) if l.split(' ', 1)[0] == prefixes[0])
            return seq[first:]

        seq = sequence('pose nourriture', ('pdr', 'pin', 'bct'))
        self.assertEqual([l.split()[0] for l in seq[:3]], ['pdr', 'pin', 'bct'])
        self.assertEqual(seq[0], f'pdr {a.id} 0')
        seq = sequence('prend nourriture', ('pgt', 'pin', 'bct'))
        self.assertEqual([l.split()[0] for l in seq[:3]], ['pgt', 'pin', 'bct'])
        self.assertEqual(seq[0], f'pgt {a.id} 0')
        seq = sequence('expulse', ('pex', 'ppo'))
        self.assertEqual(seq[:2], [f'pex {a.id}', f'ppo {b.id} 0 0 {g.players[b.id]["dir"]}'])
        seq = sequence('fork', ('pfk', 'enw'))
        self.assertEqual([l.split()[0] for l in seq], ['pfk', 'enw'])
        self.assertEqual(seq[0], f'pfk {a.id}')
        # 1x1 world: both players take part in the ritual
        seq = sequence('incantation', ('pic', 'pie', 'plv', 'bct'))
        self.assertEqual(seq[0].split()[:4], ['pic', '0', '0', '1'])
        self.assertEqual(sorted(seq[0].split()[4:]), sorted([str(a.id), str(b.id)]))
        ending = seq[1:5]
        self.assertEqual([l.split()[0] for l in ending], ['pie', 'plv', 'plv', 'bct'])
        self.assertEqual(ending[0], 'pie 0 0 1')
        self.assertEqual(sorted(ending[1:3]), sorted([f'plv {a.id} 2', f'plv {b.id} 2']))

    def test_monitor_requests(self):
        srv = self.server(x=4, y=3, t=7, teams=('red', 'blue'))
        g = self.gfx(srv)
        p = self.player(srv, g, 'blue')
        m, _ = self.monitor(srv)
        self.assertEqual(self.ask(m, 'msz', ('msz',)), 'msz 4 3')
        self.assertFormat(self.ask(m, 'bct 3 2', ('bct',)), f'bct 3 2{Q7}')
        self.assertEqual(self.ask(m, 'sgt', ('sgt',)), 'sgt 7')
        m.send('mct\n')
        tiles = [l for l in m.lines_for(0.3) if l.startswith('bct ')]
        self.assertEqual(len(tiles), 12)
        m.send('tna\n')
        self.assertEqual([l for l in m.lines_for(0.3) if l.startswith('tna ')], ['tna red', 'tna blue'])
        self.assertEqual(self.ask(m, f'ppo #{p.id}', ('ppo',)), f'ppo #{p.id} {p.x} {p.y} {p.dir}')
        self.assertEqual(self.ask(m, f'plv #{p.id}', ('plv',)), f'plv #{p.id} 1')
        self.assertFormat(self.ask(m, f'pin #{p.id}', ('pin',)), f'pin #{p.id} {p.x} {p.y}( \\d+){{7}}')
        self.assertFormat(self.ask(m, f'pin {p.id}', ('pin',)), f'pin #{p.id} {p.x} {p.y}( \\d+){{7}}')

    def test_unknown_command_and_bad_parameters(self):
        srv = self.server(x=4, y=3)
        g = self.gfx(srv)
        p = self.player(srv, g)
        m, _ = self.monitor(srv)
        for request in ('foo', 'avance', 'MSZ', 'sst'[:2]):
            with self.subTest(request=request):
                self.assertEqual(self.ask(m, request, ('suc', 'sbp')), 'suc')
        for request in ('msz 1', 'bct 4 0', 'bct 0 3', 'bct -1 0', 'bct 0', 'bct a b', 'mct x', 'tna x',
                        'ppo', f'ppo #{p.id + 100}', 'plv #x', 'pin # 1', 'sgt 3', 'sst', 'sst 0', 'sst -5',
                        'sst abc', 'sst 1 2'):
            with self.subTest(request=request):
                self.assertEqual(self.ask(m, request, ('suc', 'sbp')), 'sbp')

    def test_sst_changes_the_time_unit(self):
        srv = self.server(t=10)
        g = self.gfx(srv)
        p = self.player(srv, g)
        m, _ = self.monitor(srv)
        mark = g.mark()
        self.assertEqual(self.ask(m, 'sst 1000', ('sgt', 'sbp', 'suc')), 'sgt 1000')
        # every monitor learns the new time unit
        self.assertTrue(g.wait_line('sgt', mark, match=lambda l: l == 'sgt 1000'))
        self.assertEqual(self.ask(m, 'sgt', ('sgt',)), 'sgt 1000')
        answer, elapsed = p.timed('avance')
        self.assertEqual(answer, 'ok')
        self.assertDuration(elapsed, 7, 1000, 'avance after sst 1000')
        # the life time left keeps the same number of time units
        inv = p.inventory()
        self.assertGreaterEqual(inv['nourriture'], 9)

    def test_rotten_egg(self):
        srv = self.server(t=1000, c=1)
        g = self.gfx(srv)
        p = self.player(srv, g)
        mark = g.mark()
        self.assertEqual(p.cmd('fork'), 'ok')
        eht = g.wait_line('eht', mark, timeout=2)
        self.assertIsNotNone(eht)
        hatched = time.time()
        self.assertEqual(p.cmd('connect_nbr'), '1')
        egg = eht.split()[1]
        # nobody connects: the hatched egg rots after 1260 time units
        self.assertTrue(g.wait_line('edi', mark, timeout=3, match=lambda l: l.split()[1] == egg))
        self.assertGreater(time.time() - hatched, 1.26 * 0.85)
        # the slot given by the egg is gone (the parent itself starved meanwhile)
        refused = Player(srv.port)
        self._conns.append(refused)
        self.assertEqual(refused.join('team1')[1], 'ko')


# =============================================================================
# II.1 - The winning team: 6 players at the maximum level (scripted game)
# =============================================================================
@unittest.skipUnless(SLOW, 'slow test disabled')
class T16_Victory(ZappyCase):
    FOOD_GOAL = 30

    def feed(self, g, p, goal, stop=None):
        """Eats up to `goal`; with `stop`, keeps topping up until it is set."""
        while not (stop and stop.is_set()):
            if p.inventory()['nourriture'] >= goal:
                if stop is None:
                    return
                time.sleep(0.05)
                continue
            pos = g.find_tile(lambda t: t['nourriture'] > 0, near=(p.x, p.y))
            if pos:
                p.goto(*pos)
                p.cmd('prend nourriture')

    def collect(self, g, p, needs):
        while True:
            inv = p.inventory()
            if inv['nourriture'] < 15:
                self.feed(g, p, 25)
                continue
            missing = [s for s, n in needs.items() if inv[s] < n]
            if not missing:
                return
            pos = g.find_tile(lambda t: any(t[s] > 0 for s in missing), near=(p.x, p.y))
            p.goto(*pos)
            for s in missing:
                if g.tiles[pos][s] > 0:
                    p.cmd(f'prend {s}')

    def test_six_players_at_level_8_win(self):
        srv = self.server(x=12, y=12, t=300, teams=('winners', 'others'), c=6)
        g = self.gfx(srv)
        bots = [self.player(srv, g, 'winners') for _ in range(6)]
        needs = Counter()
        for _, stones in ELEVATION.values():
            needs.update(stones)
        collector, others = bots[0], bots[1:]

        stop, errors = threading.Event(), []

        def run(fn, *args):
            try:
                fn(*args)
            except Exception as e:     # reported by the main thread
                errors.append(e)
        threads = [threading.Thread(target=run, args=(self.feed, g, b, self.FOOD_GOAL + 10, stop)) for b in others]
        for th in threads:
            th.start()
        run(self.collect, g, collector, needs)
        run(self.feed, g, collector, self.FOOD_GOAL)
        stop.set()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        # a feeder may have been stopped while hungry: everybody eats enough
        # for the 7 rituals before joining the collector
        for b in others:
            self.feed(g, b, self.FOOD_GOAL)

        # everybody on the collector's square
        for b in others:
            b.goto(collector.x, collector.y)
        mark = g.mark()
        for level in range(1, 8):
            for stone, n in ELEVATION[level][1].items():
                for _ in range(n):
                    self.assertEqual(collector.cmd(f'pose {stone}'), 'ok')
            self.assertEqual(collector.cmd('incantation'), 'elevation en cours', f'level {level}')
            for b in others:
                self.assertEqual(b.readline(), 'elevation en cours')
            for b in bots:
                self.assertEqual(b.readline(4), f'niveau actuel : {level + 1}')
        self.assertIsNotNone(g.wait_line('seg', mark, timeout=3))
        self.assertEqual(g.winner, 'winners')
        srv.proc.wait(6)
        self.assertEqual(srv.proc.returncode, 0, 'server should stop after the victory')


# =============================================================================
# III.2 - The AI client
# =============================================================================
class T17_ClientAI(ZappyCase):
    def run_client(self, *args, timeout=5):
        return subprocess.run([sys.executable, CLIENT, *args], capture_output=True, text=True, timeout=timeout)

    def test_usage_errors(self):
        for args in ([], ['-n', 'a'], ['-p', '4242'], ['-n', 'a', '-p', 'abc'], ['-n', 'a', '-p', '0'],
                     ['-n', 'a', '-p', '70000'], ['-x']):
            with self.subTest(args=args):
                r = self.run_client(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('usage', r.stderr.lower())
                self.assertNotIn('Traceback', r.stderr)

    def test_unreachable_server(self):
        r = self.run_client('-n', 'a', '-p', str(__import__('zappy_testlib').free_port()))
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot connect', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_hostname_option(self):
        srv = self.server()
        g = self.gfx(srv)
        proc = subprocess.Popen([sys.executable, CLIENT, '-n', 'team1', '-p', str(srv.port), '-h', 'localhost'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(g.wait_for(lambda: len(g.players) == 1, 3))
        finally:
            proc.kill()
            proc.wait()

    def test_refused_team(self):
        srv = self.server()
        r = self.run_client('-n', 'unknown', '-p', str(srv.port))
        self.assertEqual(r.returncode, 1)
        self.assertIn('refused', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_requests_sent_without_waiting_for_execution(self):
        """III.5: the client sends several requests before getting any answer."""
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        proc = subprocess.Popen([sys.executable, CLIENT, '-n', 'team1', '-p', str(listener.getsockname()[1])],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            listener.settimeout(5)
            conn, _ = listener.accept()
            c = Conn.__new__(Conn)
            c.sock, c.buf, c.eof = conn, b'', False
            c.send('BIENVENUE\n')
            self.assertEqual(c.readline(), 'team1')
            c.send('3\n10 10\n')
            # a fake server that never answers: everything received was sent without waiting
            pending = c.lines_for(1.0)
            self.assertGreaterEqual(len(pending), 2, f'requests received without answers: {pending}')
            self.assertLessEqual(len(pending), 10)
            c.close()
        finally:
            proc.kill()
            proc.wait()
            listener.close()

    def test_client_never_exceeds_ten_pending_requests(self):
        """Each answer is delayed: the client must never have more than 10 requests in flight."""
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        proc = subprocess.Popen([sys.executable, CLIENT, '-n', 'team1', '-p', str(listener.getsockname()[1])],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            listener.settimeout(5)
            conn, _ = listener.accept()
            c = Conn.__new__(Conn)
            c.sock, c.buf, c.eof = conn, b'', False
            c.send('BIENVENUE\n')
            c.readline()
            c.send('3\n10 10\n')
            in_flight, worst, end = [], 0, time.time() + 3
            while time.time() < end:
                for line in c.lines_for(0.05):
                    in_flight.append(line)
                worst = max(worst, len(in_flight))
                if in_flight:                    # answer the oldest request
                    cmd = in_flight.pop(0)
                    answer = ('{nourriture 10, linemate 0, deraumere 0, sibur 0, mendiane 0, phiras 0, thystame 0}'
                              if cmd == 'inventaire' else '{nourriture, , linemate, }' if cmd == 'voir'
                              else '0' if cmd == 'connect_nbr' else 'ok')
                    c.send(answer + '\n')
            self.assertGreater(worst, 1)
            self.assertLessEqual(worst, 10)
            c.close()
        finally:
            proc.kill()
            proc.wait()
            listener.close()

    def test_client_sleeps_while_waiting(self):
        srv = self.server(t=1)
        g = self.gfx(srv)
        proc = subprocess.Popen([sys.executable, CLIENT, '-n', 'team1', '-p', str(srv.port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            g.wait_for(lambda: len(g.players) == 1, 3)
            time.sleep(0.5)
            c0 = cpu_seconds(proc.pid)
            time.sleep(2)
            self.assertLess(cpu_seconds(proc.pid) - c0, 0.05)
        finally:
            proc.kill()
            proc.wait()

    def test_client_exits_when_server_stops(self):
        srv = self.server(t=100)
        g = self.gfx(srv)
        proc = subprocess.Popen([sys.executable, CLIENT, '-n', 'team1', '-p', str(srv.port)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        g.wait_for(lambda: len(g.players) == 1, 3)
        srv.stop()
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn('Traceback', err)

    @unittest.skipUnless(SLOW, 'slow test disabled')
    def test_autonomous_team_levels_up(self):
        srv = self.server(x=10, y=10, t=500, teams=('ai',), c=6)
        g = self.gfx(srv)
        procs = [subprocess.Popen([sys.executable, CLIENT, '-n', 'ai', '-p', str(srv.port)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                 for _ in range(6)]
        try:
            start = time.time()
            reached = g.wait_for(lambda: any(p['level'] >= 3 for p in g.players.values()), 40)
            self.assertTrue(reached, 'no AI player reached level 3 in 40 s')
            self.assertTrue(g.lines('pbc'), 'the AI never broadcast')
            self.assertTrue(g.lines('pic'), 'the AI never started an incantation')
            self.assertLessEqual(len(g.lines('pdi')), 1, 'AI players starve')
            wall = time.time() - start
            for proc in procs:
                if proc.poll() is None:
                    self.assertLess(cpu_seconds(proc.pid) / wall, 0.5, 'AI client uses too much CPU')
            self.assertTrue(srv.alive())
        finally:
            srv.stop()
            for proc in procs:
                try:
                    _, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    _, err = proc.communicate()
                self.assertNotIn('Traceback', err)


# =============================================================================
# III.12 - The graphic client (needs tkinter + a display)
# =============================================================================
def tk_python():
    candidates = [sys.executable, shutil.which('python3')]
    candidates += sorted(glob.glob(os.path.expanduser('~/.local/share/uv/python/*/bin/python3')))
    for py in filter(None, candidates):
        if subprocess.run([py, '-c', 'import tkinter; tkinter.Tk().destroy()'],
                          capture_output=True).returncode == 0:
            return py
    return None


TK_PYTHON = tk_python() if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) else None

GFX_HARNESS = r'''
import importlib.machinery, json, sys, types
sys.dont_write_bytecode = True
m = importlib.machinery.SourceFileLoader('gfx', sys.argv[1]).load_module()
g = m.ZappyGFX('127.0.0.1', int(sys.argv[2]))
errors, bells = [], [0]
g.root.report_callback_exception = lambda *a: errors.append(repr(a[1]))
g.root.bell = lambda: bells.__setitem__(0, bells[0] + 1)
def click():
    p = next(iter(g.players.values()))
    g._on_click(types.SimpleNamespace(x=p['x'] * g.cell + 3 - g.canvas.canvasx(0),
                                      y=p['y'] * g.cell + 3 - g.canvas.canvasy(0)))
    g.selected_player = (p['x'], p['y'])
def done():
    print(json.dumps({'errors': errors, 'bells': bells[0], 'players': len(g.players), 'tiles': len(g.tiles),
                      'size': [g.map_width, g.map_height], 'eggs': len(g.eggs),
                      'tile_text': g.tile_text.get('1.0', 'end'), 'ranking': g.team_text.get('1.0', 'end'),
                      'log': g.bcast_text.get('1.0', 'end'), 'selected': list(g.selected or []),
                      'winner': g.winner, 'status': g.status_var.get()}))
    g.root.destroy()
g.connect()
g.root.after(int(sys.argv[3]), click)
g.root.after(int(sys.argv[3]) + 300, done)
g.root.mainloop()
'''


class T18_GraphicClient(ZappyCase):
    def test_usage_errors(self):
        for args in ([], ['-p', 'abc'], ['-h', 'localhost']):
            with self.subTest(args=args):
                r = subprocess.run([TK_PYTHON or sys.executable, GFX, *args], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn('Traceback', r.stderr)

    @unittest.skipUnless(TK_PYTHON, 'needs a python with tkinter and a display')
    def test_unreachable_server(self):
        r = subprocess.run([TK_PYTHON, GFX, '-p', str(__import__('zappy_testlib').free_port())],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('Traceback', r.stderr)

    @unittest.skipUnless(TK_PYTHON, 'needs a python with tkinter and a display')
    def test_live_game_view(self):
        srv = self.server(x=8, y=6, t=100, teams=('red', 'blue'))
        g = self.gfx(srv)
        red, blue = self.player(srv, g, 'red'), self.player(srv, g, 'blue')
        harness = tempfile.NamedTemporaryFile('w', suffix='.py', delete=False)
        harness.write(GFX_HARNESS)
        harness.close()
        proc = subprocess.Popen([TK_PYTHON, harness.name, GFX, str(srv.port), '1500'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)
        red.cmd('broadcast hello')
        blue.cmd('broadcast world')
        red.cmd('fork')
        blue.cmd('avance')
        out, err = proc.communicate(timeout=15)
        os.unlink(harness.name)
        self.assertEqual(proc.returncode, 0, err)
        state = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(state['errors'], [])
        self.assertEqual(state['size'], [8, 6])
        self.assertEqual(state['tiles'], 48)
        self.assertEqual(state['players'], 2)
        self.assertEqual(state['eggs'], 1)
        self.assertEqual(state['bells'], 2, 'one sound per broadcast')
        self.assertIn('hello', state['log'])
        self.assertIn('world', state['log'])
        self.assertIn('Square', state['tile_text'])
        self.assertIn('Players:', state['tile_text'])
        self.assertRegex(state['tile_text'], r'#\d+ \[(red|blue)\] level 1')
        self.assertIn('food', state['tile_text'])
        self.assertIn('red', state['ranking'])
        self.assertIn('blue', state['ranking'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
