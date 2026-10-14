#!/usr/bin/env python3
"""Zappy AI client: drives one autonomous player.

Once launched, the client plays alone: it feeds itself, gathers the stones of
its next elevation, gathers teammates of the same level through broadcasts and
starts the incantation. It forks (and launches a new client on the hatched egg)
while the team has fewer than 6 players.

The client sends its requests without waiting for their execution: a whole
path, or 'inventaire' and 'voir', leave in one go (never more than the 10
requests the server keeps), then the answers are read in order. The socket
stays blocking: the client sleeps in recv() until the server answers, so it
never busy-waits.
"""

import argparse
import math
import os
import random
import socket
import subprocess
import sys

STONES = ['linemate', 'deraumere', 'sibur', 'mendiane', 'phiras', 'thystame']
RESOURCES = ['nourriture'] + STONES

# level -> (players needed, stones needed) to reach level + 1
ELEVATION = {
    1: (1, {'linemate': 1}),
    2: (2, {'linemate': 1, 'deraumere': 1, 'sibur': 1}),
    3: (2, {'linemate': 2, 'sibur': 1, 'phiras': 2}),
    4: (4, {'linemate': 1, 'deraumere': 1, 'sibur': 2, 'phiras': 1}),
    5: (4, {'linemate': 1, 'deraumere': 2, 'sibur': 1, 'mendiane': 3}),
    6: (6, {'linemate': 1, 'deraumere': 2, 'sibur': 3, 'phiras': 1}),
    7: (6, {'linemate': 2, 'deraumere': 2, 'sibur': 2, 'mendiane': 2, 'phiras': 2, 'thystame': 1}),
}
MAX_LEVEL = 8
MAX_PENDING = 10       # the server ignores requests beyond 10 without answer
TEAM_GOAL = 6

FOOD_CRITICAL = 9      # below: drop everything and eat
FOOD_TO_CALL = 22      # needed to start gathering the team
FOOD_TO_JOIN = 16      # needed to answer a call
FOOD_HUNT = 26         # below: travel to food; above: only pick the food we walk on
FOOD_COMFORT = 35      # above: stop picking food up
NEAREST_CHOICES = 3    # a target is drawn among the closest useful squares

# Durations below are in time units, measured with the known cost of each command
COMMAND_COST = {'inventaire': 1, 'fork': 42, 'connect_nbr': 0, 'incantation': 300}
DEFAULT_COST = 7
CALL_TIMEOUT = 1500    # how long a caller waits for its teammates
FOLLOW_TIMEOUT = 200   # without news from the caller, a follower gives up
HEARTBEAT_EVERY = 150  # between two "alive" broadcasts
HERE_EVERY = 50        # a waiting follower reminds the caller it is there
ALIVE_WINDOW = 600     # a teammate not heard for this long is considered gone
FORK_COOLDOWN = 1000
EGG_CHECK_EVERY = 100


class Dead(Exception):
    """The player died or the server closed the connection."""


class Refused(Exception):
    """The server did not let the player join the game."""


class Connection:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port))
        self.buffer = b''

    def send(self, line):
        self.sock.sendall((line + '\n').encode())

    def send_many(self, lines):
        self.sock.sendall(''.join(line + '\n' for line in lines).encode())

    def read_line(self):
        while b'\n' not in self.buffer:
            data = self.sock.recv(4096)   # blocking: sleeps until the server talks
            if not data:
                raise Dead('connection closed by the server')
            self.buffer += data
        line, self.buffer = self.buffer.split(b'\n', 1)
        return line.decode(errors='replace').strip()


class AI:
    def __init__(self, conn, team, args):
        self.conn = conn
        self.team = team
        self.args = args
        self.id = random.randrange(1, 2 ** 31)
        self.level = 1
        self.inventory = {r: 0 for r in RESOURCES}
        self.inventory['nourriture'] = 10
        self.clock = 0            # time units spent since the connection

        self.vision = [[]]
        self.messages = []        # (K, kind, sender_id, level) from teammates
        self.pushed = False
        self.teammates = {}       # sender id -> last tick heard
        self.last_broadcast = 0
        self.last_fork = -FORK_COOLDOWN
        self.egg_pending = False
        self.last_egg_check = 0
        self.last_here = 0

        self.mode = 'explore'     # explore | call | join | wait
        self.mode_since = 0
        self.leader = None        # caller id followed in join/wait mode
        self.leader_k = None      # last direction heard from the leader
        self.leader_seen = 0
        self.helpers = set()      # ids that answered our call from our square
        self.call_cooldown = 0

    # ------------------------------------------------------------------ network
    def log(self, msg):
        print(f'[{self.team}#{self.id % 10000:04d} L{self.level}] {msg}', flush=True)

    def handle_async(self, line, awaiting_incantation=False):
        """Handles a line that is not the answer to our request. Returns True if consumed."""
        if line == 'mort':
            raise Dead('starved to death')
        if line.startswith('message '):
            self.on_message(line[len('message '):])
            return True
        if line.startswith('deplacement '):
            self.pushed = True
            return True
        if line.startswith('niveau actuel') and not awaiting_incantation:
            self.on_level(line)
            return True
        if line == 'elevation en cours' and not awaiting_incantation:
            self.log('taking part in an elevation')
            return True
        return False

    def read_answer(self, awaiting_incantation=False):
        while True:
            line = self.conn.read_line()
            if not self.handle_async(line, awaiting_incantation):
                return line

    def command(self, cmd):
        self.clock += COMMAND_COST.get(cmd, DEFAULT_COST)
        self.conn.send(cmd)
        return self.read_answer(awaiting_incantation=(cmd == 'incantation'))

    def batch(self, cmds):
        """Sends the requests without waiting for their execution (10 at most in
        flight), then returns their answers in order."""
        answers = []
        for start in range(0, len(cmds), MAX_PENDING):
            chunk = cmds[start:start + MAX_PENDING]
            self.clock += sum(COMMAND_COST.get(c, DEFAULT_COST) for c in chunk)
            self.conn.send_many(chunk)
            answers += [self.read_answer() for _ in chunk]
        return answers

    def on_level(self, line):
        try:
            new_level = int(line.split(':')[1])
        except (IndexError, ValueError):
            return
        if new_level != self.level:
            self.log(f'elevation succeeded: level {new_level}')
        self.level = new_level
        self.set_mode('explore')

    def on_message(self, payload):
        k_text, _, text = payload.partition(',')
        parts = text.split('|')
        if len(parts) != 5 or parts[0] != 'ZAPPY' or parts[1] != self.team:
            return
        try:
            k, sender, level = int(k_text), int(parts[3]), int(parts[4])
        except ValueError:
            return
        self.teammates[sender] = self.clock
        self.messages.append((k, parts[2], sender, level))

    def broadcast_cmd(self, kind):
        self.last_broadcast = self.clock
        return f'broadcast ZAPPY|{self.team}|{kind}|{self.id}|{self.level}'

    def broadcast(self, kind):
        self.command(self.broadcast_cmd(kind))

    # ------------------------------------------------------------------ perception
    def parse_inventory(self, line):
        for item in line.strip('{}').split(','):
            fields = item.split()
            if len(fields) == 2 and fields[0] in self.inventory and fields[1].isdigit():
                self.inventory[fields[0]] = int(fields[1])

    @staticmethod
    def parse_vision(line):
        if not line.startswith('{'):
            return [[]]
        return [tile.split() for tile in line.strip('{}').split(',')]

    def observe(self):
        """Inventory and field of view, asked together."""
        inventory, vision = self.batch(['inventaire', 'voir'])
        self.parse_inventory(inventory)
        self.vision = self.parse_vision(vision)

    def look(self):
        return self.parse_vision(self.command('voir'))

    # ------------------------------------------------------------------ helpers
    def food(self):
        return self.inventory['nourriture']

    def missing_stones(self):
        if self.level >= MAX_LEVEL:
            return {}
        _, needed = ELEVATION[self.level]
        return {s: n - self.inventory[s] for s, n in needed.items() if self.inventory[s] < n}

    def set_mode(self, mode, leader=None):
        self.mode = mode
        self.mode_since = self.clock
        self.leader = leader
        self.leader_k = None
        self.leader_seen = self.clock
        if mode == 'call':
            self.helpers = set()

    def team_size(self):
        return 1 + sum(1 for t in self.teammates.values() if self.clock - t <= ALIVE_WINDOW)

    @staticmethod
    def tile_offset(index):
        """Vision index -> (distance ahead, lateral offset; negative is left)."""
        d = int(math.isqrt(index))
        return d, index - d * d - d

    def go_to(self, index):
        """Sends the whole path at once."""
        ahead, side = self.tile_offset(index)
        path = ['avance'] * ahead
        if side:
            path += ['gauche' if side < 0 else 'droite'] + ['avance'] * abs(side)
        self.batch(path)

    def wander(self):
        roll = random.random()
        turn = ['gauche'] if roll < 0.15 else ['droite'] if roll < 0.3 else []
        self.batch(turn + ['avance'])

    def move_towards(self, k):
        """Moves one step toward the square numbered k (1 = front, counter-clockwise)."""
        turn = {3: ['gauche'], 4: ['gauche'], 6: ['droite'], 7: ['droite'], 5: ['droite', 'droite']}
        self.batch(turn.get(k, []) + ['avance'])

    def spawn_client(self):
        cmd = [sys.executable, os.path.abspath(__file__),
               '-n', self.team, '-p', str(self.args.p), '-h', self.args.h]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        self.log('new client launched on a hatched egg')

    # ------------------------------------------------------------------ messages
    def process_messages(self):
        messages, self.messages = self.messages, []
        for k, kind, sender, level in messages:
            if kind == 'CALL' and level == self.level:
                if self.mode == 'call' and sender < self.id:
                    # Two callers at once: the lowest id keeps calling
                    self.broadcast('DONE')
                    self.set_mode('join', sender)
                elif self.mode == 'explore' and self.food() >= FOOD_TO_JOIN and self.call_cooldown <= self.clock:
                    self.set_mode('join', sender)
                elif self.mode in ('join', 'wait') and self.leader is not None and sender < self.leader:
                    self.set_mode('join', sender)
                if self.mode in ('join', 'wait') and sender == self.leader:
                    self.leader_k = k
                    self.leader_seen = self.clock
            elif kind == 'HERE' and self.mode == 'call' and level == self.level and k == 0:
                self.helpers.add(sender)
            elif kind == 'DONE' and self.mode in ('join', 'wait') and sender == self.leader:
                self.set_mode('explore')

    # ------------------------------------------------------------------ behaviours
    def gather(self):
        vision = self.vision
        here = vision[0]
        # Everything useful on our square is taken in one go
        wanted = dict(self.missing_stones())
        wanted['nourriture'] = max(0, FOOD_COMFORT - self.food())
        takes = []
        for item in here:
            if wanted.get(item, 0) > 0:
                wanted[item] -= 1
                takes.append(item)
        if takes:
            for item, answer in zip(takes, self.batch([f'prend {item}' for item in takes])):
                if answer == 'ok':
                    self.inventory[item] += 1
            return

        # Players that just shared an elevation see the same squares: drawing the
        # target among the closest useful ones keeps them from chasing the same food.
        if self.food() < FOOD_CRITICAL:
            wanted = {'nourriture'}
        else:
            wanted = set(self.missing_stones())
            if self.food() < FOOD_HUNT:
                wanted.add('nourriture')
        targets = [i for i in range(1, len(vision)) if wanted.intersection(vision[i])]
        if not targets:
            self.wander()
            return
        targets.sort(key=lambda i: sum(map(abs, self.tile_offset(i))))
        self.go_to(random.choice(targets[:NEAREST_CHOICES]))

    def try_elevation(self):
        """Starts an elevation when we own every stone it needs."""
        if self.level >= MAX_LEVEL or self.missing_stones() or self.food() < FOOD_TO_CALL:
            return False
        if self.call_cooldown > self.clock:
            return False
        players, _ = ELEVATION[self.level]
        if players == 1:
            self.incantate()
        else:
            self.log(f'calling {players - 1} teammate(s) of level {self.level}')
            self.set_mode('call')
        return True

    def drop_stones(self):
        on_tile = self.look()[0]
        _, needed = ELEVATION[self.level]
        drops = [stone for stone, n in needed.items() for _ in range(max(0, n - on_tile.count(stone)))]
        for stone, answer in zip(drops, self.batch([f'pose {stone}' for stone in drops])):
            if answer == 'ok':
                self.inventory[stone] -= 1

    def incantate(self):
        self.drop_stones()
        answer = self.command('incantation')
        if answer != 'elevation en cours':
            self.log('incantation refused')
            self.call_cooldown = self.clock + 300
            self.set_mode('explore')
            return
        self.log('incantation started')
        while True:
            line = self.conn.read_line()
            if line.startswith('niveau actuel'):
                self.on_level(line)
                return
            self.handle_async(line)

    def calling(self):
        players, _ = ELEVATION[self.level]
        if self.food() < FOOD_CRITICAL or self.clock - self.mode_since > CALL_TIMEOUT:
            self.log('nobody came, giving up the call')
            self.broadcast('DONE')
            self.call_cooldown = self.clock + 600
            self.set_mode('explore')
            return
        _, answer = self.batch([self.broadcast_cmd('CALL'), 'voir'])
        vision = self.parse_vision(answer)
        on_tile = vision[0].count('player')
        if min(len(self.helpers), on_tile) + 1 >= players:
            self.incantate()
            if self.mode == 'call':
                self.broadcast('DONE')
        elif 'nourriture' in vision[0] and self.food() < FOOD_COMFORT:
            if self.command('prend nourriture') == 'ok':
                self.inventory['nourriture'] += 1

    def joining(self):
        if self.food() < FOOD_CRITICAL or self.clock - self.leader_seen > FOLLOW_TIMEOUT:
            self.set_mode('explore')
            return
        k, self.leader_k = self.leader_k, None
        if k is None:
            return   # no fresh direction yet: the inventory refresh let time pass
        if k == 0:
            self.set_mode('wait', self.leader)
            self.broadcast('HERE')
            return
        self.move_towards(k)

    def waiting(self):
        if self.food() < FOOD_CRITICAL or self.clock - self.leader_seen > FOLLOW_TIMEOUT:
            self.set_mode('explore')
            return
        if self.pushed or (self.leader_k is not None and self.leader_k != 0):
            self.set_mode('join', self.leader)
            return
        self.leader_k = None
        if self.clock - self.last_here >= HERE_EVERY:
            self.last_here = self.clock
            self.broadcast('HERE')

    def reproduce(self):
        if self.egg_pending:
            if self.clock - self.last_egg_check < EGG_CHECK_EVERY:
                return
            self.last_egg_check = self.clock
            answer = self.command('connect_nbr')
            if answer.isdigit() and int(answer) > 0:
                self.spawn_client()
                self.egg_pending = False
            return
        if (not self.egg_pending and self.team_size() < TEAM_GOAL
                and self.food() >= FOOD_TO_JOIN and self.clock - self.last_fork >= FORK_COOLDOWN
                and self.clock > ALIVE_WINDOW
                and all(self.id < i for i, t in self.teammates.items() if self.clock - t <= ALIVE_WINDOW)):
            if self.command('fork') == 'ok':
                self.log(f'egg laid (team size ~{self.team_size()})')
                self.last_fork = self.clock
                self.egg_pending = True

    def run(self):
        while True:
            self.observe()
            self.process_messages()
            if self.pushed and self.mode == 'call':
                self.set_mode('explore')
            self.pushed = self.pushed and self.mode == 'wait'

            if self.food() < FOOD_CRITICAL and self.mode != 'explore':
                self.set_mode('explore')
            if self.clock - self.last_broadcast >= HEARTBEAT_EVERY:
                self.broadcast('HI')

            if self.mode == 'call':
                self.calling()
            elif self.mode == 'join':
                self.joining()
            elif self.mode == 'wait':
                self.waiting()
            else:
                self.reproduce()
                if not self.try_elevation():
                    self.gather()


def parse_arguments():
    parser = argparse.ArgumentParser(
        prog='./client', add_help=False,
        usage='./client -n <team> -p <port> [-h <hostname>]')
    parser.add_argument('-n', required=True, metavar='team', help='team name')
    parser.add_argument('-p', required=True, type=int, metavar='port', help='port')
    parser.add_argument('-h', default='localhost', metavar='hostname',
                        help="name of the host, by default it'll be localhost")
    parser.add_argument('--help', action='help', help='show this help message and exit')
    args = parser.parse_args()
    if not 0 < args.p < 65536:
        parser.error('port must be between 1 and 65535')
    return args


def handshake(conn, team):
    if conn.read_line() != 'BIENVENUE':
        raise Refused('unexpected greeting')
    conn.send(team)
    slots = conn.read_line()
    if not slots.lstrip('-').isdigit():
        raise Refused(f'team "{team}" refused by the server ({slots})')
    size = conn.read_line().split()
    print(f'[{team}] connected: {slots} slot(s) left, world {size[0]}x{size[1]}', flush=True)


def main():
    args = parse_arguments()
    try:
        conn = Connection(args.h, args.p)
    except OSError as e:
        print(f'Error: cannot connect to {args.h}:{args.p}: {e}', file=sys.stderr)
        return 1
    try:
        handshake(conn, args.n)
        AI(conn, args.n, args).run()
    except Refused as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1
    except Dead as e:
        print(f'[{args.n}] {e}', flush=True)
        return 0
    except (OSError, IndexError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
