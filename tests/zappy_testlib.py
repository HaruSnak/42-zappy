"""Helpers for the Zappy test battery: server process, player and graphic
connections, and an independent reference model of the rules (vision,
sound direction) written from the subject, not from the server code."""

import os
import select
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, 'server')
CLIENT = os.path.join(ROOT, 'client')
GFX = os.path.join(ROOT, 'gfx')

RESOURCES = ['nourriture', 'linemate', 'deraumere', 'sibur', 'mendiane', 'phiras', 'thystame']
STONES = RESOURCES[1:]
# level -> (players, stones) to reach level + 1 (subject's elevation table)
ELEVATION = {
    1: (1, {'linemate': 1}),
    2: (2, {'linemate': 1, 'deraumere': 1, 'sibur': 1}),
    3: (2, {'linemate': 2, 'sibur': 1, 'phiras': 2}),
    4: (4, {'linemate': 1, 'deraumere': 1, 'sibur': 2, 'phiras': 1}),
    5: (4, {'linemate': 1, 'deraumere': 2, 'sibur': 1, 'mendiane': 3}),
    6: (6, {'linemate': 1, 'deraumere': 2, 'sibur': 3, 'phiras': 1}),
    7: (6, {'linemate': 2, 'deraumere': 2, 'sibur': 2, 'mendiane': 2, 'phiras': 2, 'thystame': 1}),
}
# Graphic orientation: 1=N 2=E 3=S 4=W, y grows southwards
DIRS = {1: (0, -1), 2: (1, 0), 3: (0, 1), 4: (-1, 0)}


class Closed(Exception):
    pass


class Dead(Exception):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def cpu_seconds(pid):
    with open(f'/proc/{pid}/stat') as f:
        fields = f.read().rsplit(')', 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')


def thread_count(pid):
    with open(f'/proc/{pid}/status') as f:
        for line in f:
            if line.startswith('Threads:'):
                return int(line.split()[1])
    return -1


# --------------------------------------------------------------------- server
class ServerProc:
    def __init__(self, x=10, y=10, teams=('team1', 'team2'), c=5, t=100, args=None, port=None):
        self.port = port or free_port()
        self.x, self.y, self.t, self.c = x, y, t, c
        self.teams = list(teams)
        self.logfile = tempfile.NamedTemporaryFile(prefix='zappy_srv_', suffix='.log', delete=False)
        if args is None:
            args = ['-p', str(self.port), '-x', str(x), '-y', str(y), '-n', *teams, '-c', str(c)]
            if t is not None:
                args += ['-t', str(t)]
        self.proc = subprocess.Popen([SERVER, *args], stdout=self.logfile, stderr=subprocess.STDOUT, cwd=ROOT)
        self.ready = self._wait_ready()

    def _wait_ready(self, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                return False
            if 'Server listening' in self.output():
                return True
            time.sleep(0.02)
        return False

    def output(self):
        with open(self.logfile.name, errors='replace') as f:
            return f.read()

    @property
    def pid(self):
        return self.proc.pid

    def alive(self):
        return self.proc.poll() is None

    def stop(self, sig=signal.SIGINT, timeout=5.0):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        self.logfile.close()
        try:
            os.unlink(self.logfile.name)
        except OSError:
            pass


# ----------------------------------------------------------------- connection
class Conn:
    def __init__(self, port, host='127.0.0.1'):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(None)
        self.buf = b''
        self.eof = False

    def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.sock.sendall(data)

    def readline(self, timeout=3.0):
        """Next line without '\\n', or None on timeout. Raises Closed on EOF."""
        end = time.time() + timeout
        while b'\n' not in self.buf:
            if self.eof:
                raise Closed()
            remaining = end - time.time()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                return None
            try:
                data = self.sock.recv(65536)
            except OSError:
                data = b''
            if not data:
                self.eof = True
                continue
            self.buf += data
        line, self.buf = self.buf.split(b'\n', 1)
        return line.decode(errors='replace')

    def lines_for(self, duration):
        """All lines received during `duration` seconds."""
        out, end = [], time.time() + duration
        while True:
            try:
                line = self.readline(max(0.0, end - time.time()))
            except Closed:
                break
            if line is None:
                break
            out.append(line)
        return out

    def wait_closed(self, timeout=3.0):
        end = time.time() + timeout
        try:
            while time.time() < end:
                self.readline(max(0.01, end - time.time()))
        except Closed:
            return True
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Player(Conn):
    """A player driven by the tests. Position and orientation are tracked
    locally (initial values come from the graphic 'pnw' event)."""

    def __init__(self, port):
        super().__init__(port)
        self.id = None
        self.x = self.y = 0
        self.dir = 1
        self.level = 1
        self.w = self.h = 0
        self.notifications = []

    def join(self, team):
        welcome = self.readline()
        self.send(team + '\n')
        slots = self.readline()
        if slots is None or not slots.lstrip('-').isdigit():
            return welcome, slots, None
        dims = self.readline()
        self.w, self.h = map(int, dims.split())
        return welcome, slots, dims

    def cmd(self, command, timeout=3.0):
        """Sends a request and returns its answer. Broadcast messages and kick
        notifications received meanwhile are kept in self.notifications."""
        self.send(command + '\n')
        while True:
            line = self.readline(timeout)
            if line == 'mort':
                raise Dead()
            if line is not None and line.startswith(('message ', 'deplacement ')):
                self.notifications.append(line)
                continue
            return line

    def timed(self, command, timeout=5.0):
        start = time.time()
        line = self.cmd(command, timeout)
        return line, time.time() - start

    # movement with local tracking
    def turn_to(self, d):
        while self.dir != d:
            if (d - self.dir) % 4 == 3:
                assert self.cmd('gauche') == 'ok'
                self.dir = (self.dir - 2) % 4 + 1
            else:
                assert self.cmd('droite') == 'ok'
                self.dir = self.dir % 4 + 1

    def advance(self):
        assert self.cmd('avance') == 'ok'
        dx, dy = DIRS[self.dir]
        self.x, self.y = (self.x + dx) % self.w, (self.y + dy) % self.h

    def goto(self, tx, ty):
        dx, dy = shortest(tx - self.x, self.w), shortest(ty - self.y, self.h)
        if dx:
            self.turn_to(2 if dx > 0 else 4)
            for _ in range(abs(dx)):
                self.advance()
        if dy:
            self.turn_to(3 if dy > 0 else 1)
            for _ in range(abs(dy)):
                self.advance()

    def inventory(self):
        line = self.cmd('inventaire')
        inv = {}
        for item in line.strip('{}').split(','):
            name, value = item.split()
            inv[name] = int(value)
        return inv


# ------------------------------------------------------------ graphic observer
def strip_ids(line):
    """'pnw #3 ...' -> 'pnw 3 ...': player and egg numbers are written "#n"."""
    return ' '.join(w[1:] if w.startswith('#') and w[1:].isdigit() else w for w in line.split(' '))


class GfxObserver(Conn):
    """Graphic client that keeps a model of the world from the server events.
    self.events holds the lines with the '#' of the numbers removed,
    self.raw the lines exactly as sent by the server."""

    def __init__(self, port):
        super().__init__(port)
        self.lock = threading.Condition()
        self.events = []
        self.raw = []
        self.w = self.h = None
        self.t = None
        self.teams = []
        self.tiles = {}
        self.players = {}
        self.eggs = {}
        self.winner = None
        self.send('GRAPHIC\n')
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            try:
                line = self.readline(3600)
            except Closed:
                break
            if line is None:
                continue
            with self.lock:
                self.raw.append(line)
                line = strip_ids(line)
                self.events.append((time.time(), line))
                self._apply(line.split())
                self.lock.notify_all()

    def _apply(self, p):
        if not p:
            return
        c, a = p[0], p[1:]
        try:
            if c == 'msz':
                self.w, self.h = int(a[0]), int(a[1])
            elif c == 'sgt':
                self.t = int(a[0])
            elif c == 'tna':
                self.teams.append(a[0])
            elif c == 'bct':
                self.tiles[(int(a[0]), int(a[1]))] = dict(zip(RESOURCES, map(int, a[2:9])))
            elif c == 'pnw':
                self.players[int(a[0])] = {'x': int(a[1]), 'y': int(a[2]), 'dir': int(a[3]),
                                           'level': int(a[4]), 'team': a[5], 'inv': None}
                # pnw has no inventory: the monitor asks for it
                self.send(f'pin #{a[0]}\n')
            elif c == 'ppo' and int(a[0]) in self.players:
                self.players[int(a[0])].update(x=int(a[1]), y=int(a[2]), dir=int(a[3]))
            elif c == 'plv' and int(a[0]) in self.players:
                self.players[int(a[0])]['level'] = int(a[1])
            elif c == 'pin' and int(a[0]) in self.players:
                self.players[int(a[0])]['inv'] = dict(zip(RESOURCES, map(int, a[3:10])))
            elif c == 'pdi':
                self.players.pop(int(a[0]), None)
            elif c == 'enw':
                self.eggs[int(a[0])] = {'x': int(a[2]), 'y': int(a[3]), 'hatched': False, 'parent': int(a[1])}
            elif c == 'eht' and int(a[0]) in self.eggs:
                self.eggs[int(a[0])]['hatched'] = True
            elif c in ('ebo', 'edi'):
                self.eggs.pop(int(a[0]), None)
            elif c == 'seg':
                self.winner = a[0]
        except (ValueError, IndexError):
            pass

    def wait_for(self, predicate, timeout=3.0):
        end = time.time() + timeout
        with self.lock:
            while not predicate():
                remaining = end - time.time()
                if remaining <= 0:
                    return False
                self.lock.wait(remaining)
            return True

    def mark(self):
        with self.lock:
            return len(self.events)

    def lines(self, prefix=None, since=0):
        with self.lock:
            return [l for _, l in self.events[since:] if prefix is None or l.split(' ', 1)[0] == prefix]

    def wait_line(self, prefix, since=0, timeout=3.0, match=None):
        found = []

        def pred():
            found[:] = [l for _, l in self.events[since:]
                        if l.split(' ', 1)[0] == prefix and (match is None or match(l))]
            return bool(found)
        return found[0] if self.wait_for(pred, timeout) else None

    def ready(self, timeout=3.0):
        return self.wait_for(lambda: self.w is not None and len(self.tiles) == self.w * self.h, timeout)

    def totals(self):
        with self.lock:
            tot = Counter()
            for tile in self.tiles.values():
                tot.update(tile)
            return tot

    def find_tile(self, predicate, near=None):
        with self.lock:
            cells = [pos for pos, tile in self.tiles.items() if predicate(tile)]
        if not cells:
            return None
        if near is None:
            return cells[0]
        px, py = near
        return min(cells, key=lambda c: abs(shortest(c[0] - px, self.w)) + abs(shortest(c[1] - py, self.h)))


def join_player(srv, gfx, team='team1'):
    """Connects a player and learns its id, position and orientation from the gfx."""
    # Tests join players one at a time: the first 'pnw' after the mark is ours
    with gfx.lock:
        mark = len(gfx.events)
    p = Player(srv.port)
    welcome, slots, dims = p.join(team)
    assert welcome == 'BIENVENUE', welcome
    assert dims is not None, f'team {team} refused: {slots}'

    line = gfx.wait_line('pnw', mark, timeout=3)
    assert line, 'no pnw for the new player'
    a = line.split()
    p.id, p.x, p.y, p.dir, p.level = int(a[1]), int(a[2]), int(a[3]), int(a[4]), int(a[5])
    return p


# ------------------------------------------------------------ reference rules
def shortest(d, n):
    d %= n
    return d - n if d > n / 2 else d


def sign(v):
    return (v > 0) - (v < 0)


def expected_sound_k(rx, ry, rdir, ex, ey, w, h):
    """Square number the sound comes from, for straight or diagonal lines:
    1 in front, then counter-clockwise; 0 on the same square."""
    dx, dy = shortest(ex - rx, w), shortest(ey - ry, h)
    if dx == 0 and dy == 0:
        return 0
    forward, left = {1: (-dy, -dx), 2: (dx, -dy), 3: (dy, dx), 4: (-dx, dy)}[rdir]
    return {(1, 0): 1, (1, 1): 2, (0, 1): 3, (-1, 1): 4,
            (-1, 0): 5, (-1, -1): 6, (0, -1): 7, (1, -1): 8}[(sign(forward), sign(left))]


def vision_squares(px, py, pdir, level, w, h):
    """Squares seen, row by row (nearest first), from the player's left to right."""
    out = []
    for d in range(level + 1):
        for off in range(-d, d + 1):
            x, y = {1: (px + off, py - d), 2: (px + d, py + off),
                    3: (px - off, py + d), 4: (px - d, py - off)}[pdir]
            out.append((x % w, y % h))
    return out


def parse_vision(line):
    assert line.startswith('{') and line.endswith('}'), line
    return [Counter(tile.split()) for tile in line[1:-1].split(',')]
