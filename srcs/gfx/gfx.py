#!/usr/bin/env python3
"""Zappy graphical client - real-time 2D view of the game (tkinter).

Usage: ./gfx -p <port> [-h <hostname>]

The client identifies itself with "GRAPHIC" and follows the server/monitor
protocol (protocol-gfx.pdf): it applies the events of the server and asks for
what it needs (pin #n, bct X Y, sst T). The socket is watched by the Tk event
loop (no thread, no polling).

Keys: m mutes the sound, + / - double or halve the time unit (sst).
"""

import argparse
import itertools
import socket
import sys
import time
from collections import defaultdict

try:
    import tkinter as tk
    from tkinter import scrolledtext
except ImportError:
    sys.exit('Error: the gfx needs tkinter (Tk for Python), e.g. "apt install python3-tk" or "pacman -S tk"')

RESOURCES = ['food', 'linemate', 'deraumere', 'sibur', 'mendiane', 'phiras', 'thystame']
RESOURCE_COLORS = {
    'food':      '#22c55e',
    'linemate':  '#3b82f6',
    'deraumere': '#f59e0b',
    'sibur':     '#ef4444',
    'mendiane':  '#8b5cf6',
    'phiras':    '#ec4899',
    'thystame':  '#14b8a6',
}
TEAM_PALETTE = ['#ff6b6b', '#feca57', '#48dbfb', '#ff9ff3',
                '#54a0ff', '#a29bfe', '#00d2d3', '#ff9f43']
DIR_NAMES = {1: 'N', 2: 'E', 3: 'S', 4: 'W'}
DIR_VECTORS = {1: (0, -1), 2: (1, 0), 3: (0, 1), 4: (-1, 0)}
MAX_LEVEL = 8
WINNING_PLAYERS = 6

BG = '#0f0f1a'
PANEL = '#16213e'
BOX = '#0d1b2a'
TEXT = '#e0e0e0'
ACCENT = '#a0c4ff'


class ZappyGFX:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.buffer = b''

        self.map_width = 0
        self.map_height = 0
        self.cell = 48
        self.time_unit = None
        self.tiles = {}           # (x, y) -> {resource: count}
        self.players = {}         # id -> {x, y, dir, level, team, inv}
        self.eggs = {}            # id -> {x, y, hatched}
        self.incantations = {}    # (x, y) -> level
        self.teams = []
        self.team_colors = {}
        self.selected = None
        self.winner = None
        self.redraw_pending = False
        self.last_redraw = 0.0
        self.anim_ids = itertools.count()
        self.sound_on = True

        self.root = tk.Tk()
        self.root.title('Zappy - Graphical Client')
        self.root.configure(bg=BG)
        self.root.geometry('1200x800')
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill=tk.BOTH, expand=True)

        canvas_frame = tk.Frame(top, bg=BG)
        canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(canvas_frame, bg='#1a1a2e', cursor='crosshair', highlightthickness=0)
        h_sb = tk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        v_sb = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=h_sb.set, yscrollcommand=v_sb.set)
        v_sb.pack(side=tk.RIGHT, fill=tk.Y)
        h_sb.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind('<Button-1>', self._on_click)
        self.root.bind('<KeyPress-m>', self._toggle_sound)
        self.root.bind('<KeyPress-plus>', lambda _e: self._change_speed(2))
        self.root.bind('<KeyPress-KP_Add>', lambda _e: self._change_speed(2))
        self.root.bind('<KeyPress-minus>', lambda _e: self._change_speed(0.5))
        self.root.bind('<KeyPress-KP_Subtract>', lambda _e: self._change_speed(0.5))

        right = tk.Frame(top, bg=PANEL, width=330)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        def section(label):
            tk.Label(right, text=label, bg=PANEL, fg=ACCENT,
                     font=('DejaVu Sans Mono', 10, 'bold')).pack(pady=(8, 2), padx=6, anchor='w')

        def box(height):
            t = tk.Text(right, height=height, bg=BOX, fg=TEXT, font=('DejaVu Sans Mono', 9),
                        relief=tk.FLAT, bd=4, wrap=tk.NONE)
            t.pack(padx=6, pady=2, fill=tk.X)
            t.configure(state=tk.DISABLED)
            return t

        section('Ranking')
        self.team_text = box(8)
        section('Selected square (click on the map)')
        self.tile_text = box(14)
        section('Broadcasts')
        self.bcast_text = scrolledtext.ScrolledText(
            right, height=8, bg=BOX, fg='#a0ffa0', font=('DejaVu Sans Mono', 8), relief=tk.FLAT, bd=4)
        self.bcast_text.pack(padx=6, pady=2, fill=tk.BOTH, expand=True)
        self.bcast_text.configure(state=tk.DISABLED)

        legend = tk.Frame(right, bg=PANEL)
        legend.pack(fill=tk.X, padx=6, pady=4)
        for i, res in enumerate(RESOURCES):
            dot = tk.Canvas(legend, width=10, height=10, bg=PANEL, highlightthickness=0)
            dot.create_oval(1, 1, 9, 9, fill=RESOURCE_COLORS[res], outline='')
            dot.grid(row=i // 4, column=(i % 4) * 2, padx=(4, 1))
            tk.Label(legend, text=res, fg=TEXT, bg=PANEL, font=('DejaVu Sans Mono', 8)).grid(
                row=i // 4, column=(i % 4) * 2 + 1, sticky='w')

        self.status_var = tk.StringVar(value='Connecting...')
        self.sound_var = tk.StringVar(value='sound: on  (m to mute)   speed: + / -')
        tk.Label(self.root, textvariable=self.sound_var, bg=BG, fg='#aaaaaa',
                 font=('DejaVu Sans Mono', 9), anchor='e').pack(side=tk.BOTTOM, fill=tk.X, padx=4)
        tk.Label(self.root, textvariable=self.status_var, bg=BG, fg='#aaaaaa',
                 font=('DejaVu Sans Mono', 9), anchor='w').pack(side=tk.BOTTOM, fill=tk.X, padx=4, pady=2)

    @staticmethod
    def _set_text(widget, content):
        widget.configure(state=tk.NORMAL)
        widget.delete('1.0', tk.END)
        widget.insert(tk.END, content)
        widget.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------ network
    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port))
            self.sock.sendall(b'GRAPHIC\n')
        except OSError as exc:
            print(f'Error: cannot connect to {self.host}:{self.port}: {exc}', file=sys.stderr)
            sys.exit(1)
        self.status_var.set(f'Connected to {self.host}:{self.port}')
        # The Tk event loop calls us when the socket is readable
        self.root.tk.createfilehandler(self.sock, tk.READABLE, self._on_readable)

    def _request(self, line):
        """Monitor -> server request (msz, bct, mct, tna, ppo, plv, pin, sgt, sst)."""
        if self.sock is None:
            return
        try:
            self.sock.sendall((line + '\n').encode())
        except OSError:
            pass

    def _on_readable(self, _sock, _mask):
        try:
            data = self.sock.recv(65536)
        except OSError:
            data = b''
        if not data:
            self.root.tk.deletefilehandler(self.sock)
            self.sock.close()
            self.sock = None
            end = f'Game over - {self.winner} wins. ' if self.winner else ''
            self.status_var.set(end + 'Disconnected from server')
            return
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b'\n')
        for line in lines:
            self._parse_line(line.decode(errors='replace').strip())
        self._schedule_redraw()

    # ------------------------------------------------------------------ protocol
    def _team_color(self, team):
        if team not in self.team_colors:
            if team not in self.teams:
                self.teams.append(team)
            self.team_colors[team] = TEAM_PALETTE[(len(self.team_colors)) % len(TEAM_PALETTE)]
        return self.team_colors[team]

    def _parse_line(self, line):
        parts = line.split()
        if not parts:
            return
        # Player and egg numbers are written "#n"
        cmd, args = parts[0], [a[1:] if a.startswith('#') and a[1:].isdigit() else a for a in parts[1:]]
        try:
            handler = getattr(self, f'_on_{cmd}', None)
            if handler:
                handler(args, line)
        except (ValueError, IndexError, KeyError):
            pass

    def _on_msz(self, a, _):
        self.map_width, self.map_height = int(a[0]), int(a[1])
        self.cell = max(20, min(64, 900 // max(self.map_width, self.map_height)))
        self.canvas.configure(scrollregion=(0, 0, self.map_width * self.cell, self.map_height * self.cell))
        self.canvas.delete('all')
        for y in range(self.map_height):
            for x in range(self.map_width):
                self._draw_tile(x, y)

    def _toggle_sound(self, _event=None):
        self.sound_on = not self.sound_on
        sound = 'sound: on  (m to mute)' if self.sound_on else 'sound: muted  (m to unmute)'
        self.sound_var.set(sound + '   speed: + / -')

    def _change_speed(self, factor):
        if self.time_unit:
            self._request(f'sst {max(1, min(1000000, int(self.time_unit * factor)))}')

    def _on_sgt(self, a, _):
        self.time_unit = int(a[0])
        self.status_var.set(f'Connected to {self.host}:{self.port}   t={self.time_unit}')

    def _on_tna(self, a, _):
        self._team_color(a[0])

    def _on_bct(self, a, _):
        x, y = int(a[0]), int(a[1])
        self.tiles[(x, y)] = {res: int(v) for res, v in zip(RESOURCES, a[2:9])}
        self._draw_tile(x, y)

    def _on_pnw(self, a, _):
        self._team_color(a[5])
        self.players[int(a[0])] = {'x': int(a[1]), 'y': int(a[2]), 'dir': int(a[3]),
                                   'level': int(a[4]), 'team': a[5], 'inv': None}
        # pnw has no inventory: ask for it
        self._request(f'pin #{a[0]}')

    def _on_ppo(self, a, _):
        p = self.players.get(int(a[0]))
        if p:
            p.update(x=int(a[1]), y=int(a[2]), dir=int(a[3]))

    def _on_plv(self, a, _):
        p = self.players.get(int(a[0]))
        if p:
            p['level'] = int(a[1])

    def _on_pin(self, a, _):
        p = self.players.get(int(a[0]))
        if p:
            p.update(x=int(a[1]), y=int(a[2]))
            p['inv'] = {res: int(v) for res, v in zip(RESOURCES, a[3:10])}

    def _on_pdi(self, a, _):
        p = self.players.pop(int(a[0]), None)
        if p:
            self._log(f'x player #{a[0]} [{p["team"]}] died')

    def _on_pex(self, a, _):
        p = self.players.get(int(a[0]))
        if p:
            self._flash(p['x'], p['y'], '#ffffff', 'kick!')

    def _on_pfk(self, a, _):
        p = self.players.get(int(a[0]))
        if p:
            self._flash(p['x'], p['y'], '#fff8dc', 'egg...')

    def _on_pgt(self, a, _):
        self._on_resource_moved(a, 'took')

    def _on_pdr(self, a, _):
        self._on_resource_moved(a, 'dropped')

    def _on_resource_moved(self, a, verb):
        # The pin and bct that follow carry the new quantities
        p = self.players.get(int(a[0]))
        if p and int(a[1]) != 0:
            self._log(f'  #{a[0]} [{p["team"]}] {verb} {RESOURCES[int(a[1])]}')

    def _on_pbc(self, a, line):
        pid = int(a[0])
        text = line.split(' ', 2)[2] if len(a) > 1 else ''
        p = self.players.get(pid)
        team = p['team'] if p else '?'
        self._log(f'~ #{pid} [{team}] {text}')
        # Each broadcast is heard: audible bell + visual waves on the map
        if self.sound_on:
            self.root.bell()
        if p:
            self._sound_wave(p['x'], p['y'], self.team_colors.get(team, '#ffffff'))

    def _on_pic(self, a, _):
        x, y, level = int(a[0]), int(a[1]), int(a[2])
        self.incantations[(x, y)] = level
        self._log(f'* incantation level {level} -> {level + 1} on ({x},{y}) by {len(a) - 3} player(s)')
        self._draw_tile(x, y)

    def _on_pie(self, a, _):
        x, y, ok = int(a[0]), int(a[1]), a[2] == '1'
        self.incantations.pop((x, y), None)
        self._draw_tile(x, y)
        self._flash(x, y, '#44ff88' if ok else '#ff4444', 'UP!' if ok else 'fail')

    def _on_enw(self, a, _):
        self.eggs[int(a[0])] = {'x': int(a[2]), 'y': int(a[3]), 'hatched': False}

    def _on_eht(self, a, _):
        if int(a[0]) in self.eggs:
            self.eggs[int(a[0])]['hatched'] = True

    def _on_ebo(self, a, _):
        self.eggs.pop(int(a[0]), None)

    def _on_edi(self, a, _):
        egg = self.eggs.pop(int(a[0]), None)
        if egg:
            self._log(f'x egg #{a[0]} rotted on ({egg["x"]},{egg["y"]})')

    def _on_smg(self, _a, line):
        self._log(f'[server] {line[4:]}')

    def _on_suc(self, _a, _line):
        self.status_var.set('server: unknown command (suc)')

    def _on_sbp(self, _a, _line):
        self.status_var.set('server: bad parameters (sbp)')

    def _on_seg(self, a, _):
        self.winner = a[0]
        self._log(f'## GAME OVER - team {self.winner} wins! ##')
        self.status_var.set(f'Game over - {self.winner} wins!')

    # ------------------------------------------------------------------ drawing
    def _schedule_redraw(self):
        """Redraws at once after a batch of server data, at most every 30 ms: a
        burst of messages is then drawn by one deferred call. The refresh never
        depends on idle callbacks alone."""
        if self.redraw_pending:
            return
        wait = 30 - int((time.monotonic() - self.last_redraw) * 1000)
        if wait <= 0:
            self._redraw()
        else:
            self.redraw_pending = True
            self.root.after(wait, self._redraw)

    def _redraw(self):
        self.redraw_pending = False
        self.last_redraw = time.monotonic()
        self._draw_entities()
        self._update_ranking()
        if self.selected:
            self._show_tile_info(*self.selected)

    def _draw_tile(self, x, y):
        c = self.cell
        cx, cy = x * c, y * c
        tag = f't_{x}_{y}'
        self.canvas.delete(tag)
        bg = '#1a1a2e' if (x + y) % 2 == 0 else '#16213e'
        self.canvas.create_rectangle(cx, cy, cx + c, cy + c, fill=bg, outline='#2a2a4a', tags=('tile', tag))

        # One dot per resource kind, with its quantity when above 1
        col, row = 3, 3
        for res in RESOURCES:
            count = self.tiles.get((x, y), {}).get(res, 0)
            if count <= 0:
                continue
            self.canvas.create_oval(cx + col, cy + row, cx + col + 7, cy + row + 7,
                                    fill=RESOURCE_COLORS[res], outline='', tags=('tile', tag))
            if count > 1 and c >= 32:
                self.canvas.create_text(cx + col + 8, cy + row + 3, text=str(count), fill='#cccccc',
                                        font=('DejaVu Sans Mono', 6), anchor='w', tags=('tile', tag))
            col += 16 if c >= 32 else 9
            if col > c - 10:
                col, row = 3, row + 10

        if (x, y) in self.incantations:
            self.canvas.create_rectangle(cx + 2, cy + 2, cx + c - 2, cy + c - 2, outline='#ffd700',
                                         width=3, dash=(4, 2), tags=('tile', tag))
        if self.selected == (x, y):
            self.canvas.create_rectangle(cx + 1, cy + 1, cx + c - 1, cy + c - 1, outline='#ffff44',
                                         width=2, tags=('tile', tag))
        self.canvas.tag_lower(tag)

    def _draw_entities(self):
        self.canvas.delete('entity')
        c = self.cell
        for egg in self.eggs.values():
            ex, ey = egg['x'] * c + c - 10, egg['y'] * c + c - 12
            self.canvas.create_oval(ex, ey, ex + 7, ey + 10, fill='#fff8dc' if egg['hatched'] else '#bba77a',
                                    outline='', tags='entity')

        # Several players on a square are spread so that each one stays visible
        by_tile = defaultdict(list)
        for pid, p in self.players.items():
            by_tile[(p['x'], p['y'])].append((pid, p))
        r = max(6, c // 5)
        for (x, y), group in by_tile.items():
            for i, (pid, p) in enumerate(group):
                ox = (i % 3 - 1) * (r + 2) if len(group) > 1 else 0
                oy = (i // 3) * (r + 2) - (r // 2 if len(group) > 1 else 0)
                px, py = x * c + c // 2 + ox, y * c + c // 2 + oy
                color = self.team_colors.get(p['team'], '#ffffff')
                self.canvas.create_oval(px - r, py - r, px + r, py + r, fill=color, outline='white', tags='entity')
                # Orientation: a white tick pointing where the player looks
                vx, vy = DIR_VECTORS.get(p['dir'], (0, -1))
                self.canvas.create_line(px + vx * (r // 2), py + vy * (r // 2), px + vx * (r + 4), py + vy * (r + 4),
                                        fill='white', width=2, arrow=tk.LAST, arrowshape=(4, 5, 3), tags='entity')
                self.canvas.create_text(px, py, text=str(p['level']), fill='black',
                                        font=('DejaVu Sans Mono', max(6, r - 1), 'bold'), tags='entity')

        if self.winner:
            x0, y0 = self.canvas.canvasx(0), self.canvas.canvasy(0)
            self.canvas.create_text(x0 + self.canvas.winfo_width() // 2, y0 + 40,
                                    text=f'{self.winner} WINS!', fill='#ffd700',
                                    font=('DejaVu Sans', 28, 'bold'), tags='entity')

    def _sound_wave(self, x, y, color):
        """Broadcast visualization: rings expanding from the emitter's square."""
        c = self.cell
        self._animate_wave(x * c + c // 2, y * c + c // 2, color, f'wave_{next(self.anim_ids)}', 0)

    def _animate_wave(self, cx, cy, color, tag, step):
        self.canvas.delete(tag)
        if step >= 6:
            return
        radius = self.cell * (0.5 + step * 0.6)
        width = max(1, 4 - step // 2)
        self.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                outline=color, width=width, dash=(6, 4) if step > 3 else None, tags=tag)
        self.root.after(60, self._animate_wave, cx, cy, color, tag, step + 1)

    def _flash(self, x, y, color, text):
        c = self.cell
        tag = f'flash_{next(self.anim_ids)}'
        self.canvas.create_rectangle(x * c, y * c, x * c + c, y * c + c, outline=color, width=3, tags=tag)
        self.canvas.create_text(x * c + c // 2, y * c - 6, text=text, fill=color,
                                font=('DejaVu Sans Mono', 9, 'bold'), tags=tag)
        self.root.after(700, self.canvas.delete, tag)

    # ------------------------------------------------------------------ panels
    def _update_ranking(self):
        stats = {team: [] for team in self.teams}
        for p in self.players.values():
            stats.setdefault(p['team'], []).append(p['level'])

        def score(team):
            lv = stats[team]
            return (sum(1 for l in lv if l >= MAX_LEVEL), max(lv, default=0), sum(lv))

        lines = []
        for rank, team in enumerate(sorted(stats, key=score, reverse=True), 1):
            lv = stats[team]
            per_level = ' '.join(f'{lvl}:{lv.count(lvl)}' for lvl in range(1, MAX_LEVEL + 1) if lv.count(lvl))
            crown = ' (WINNER)' if team == self.winner else ''
            lines.append(f'{rank}. {team}{crown}  players={len(lv)}  max=L{max(lv, default=0)}')
            lines.append(f'   L8: {score(team)[0]}/{WINNING_PLAYERS}   levels {per_level or "-"}')
        self._set_text(self.team_text, '\n'.join(lines))

    def _on_click(self, event):
        tx = int(self.canvas.canvasx(event.x) // self.cell)
        ty = int(self.canvas.canvasy(event.y) // self.cell)
        if 0 <= tx < self.map_width and 0 <= ty < self.map_height:
            # Fresh data for the selected square and the players standing on it
            self._request(f'bct {tx} {ty}')
            for pid, p in self.players.items():
                if (p['x'], p['y']) == (tx, ty):
                    self._request(f'pin #{pid}')
            previous, self.selected = self.selected, (tx, ty)
            if previous:
                self._draw_tile(*previous)
            self._draw_tile(tx, ty)
            self._show_tile_info(tx, ty)

    def _show_tile_info(self, x, y):
        tile = self.tiles.get((x, y), {})
        out = [f'Square ({x}, {y})']
        if (x, y) in self.incantations:
            out.append(f'  * incantation in progress (level {self.incantations[(x, y)]})')
        out.append('Resources:')
        res = [f'{r} {tile.get(r, 0)}' for r in RESOURCES if tile.get(r, 0)]
        out.append('  ' + (', '.join(res) if res else 'none'))
        eggs = [e for e in self.eggs.values() if (e['x'], e['y']) == (x, y)]
        if eggs:
            out.append(f'Eggs: {len(eggs)} ({sum(e["hatched"] for e in eggs)} hatched)')
        out.append('Players:')
        found = False
        for pid, p in self.players.items():
            if (p['x'], p['y']) != (x, y):
                continue
            found = True
            out.append(f'  #{pid} [{p["team"]}] level {p["level"]}, facing {DIR_NAMES.get(p["dir"], "?")}')
            if p['inv']:
                inv = p['inv']
                out.append(f'    food {inv["food"]} | ' + ' '.join(f'{r[:3]} {inv[r]}' for r in RESOURCES[1:]))
        if not found:
            out.append('  none')
        self._set_text(self.tile_text, '\n'.join(out))

    def _log(self, msg):
        self.bcast_text.configure(state=tk.NORMAL)
        self.bcast_text.insert(tk.END, msg + '\n')
        if int(self.bcast_text.index('end-1c').split('.')[0]) > 500:
            self.bcast_text.delete('1.0', '100.0')
        self.bcast_text.see(tk.END)
        self.bcast_text.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------ main
    def run(self):
        self.connect()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        if self.sock:
            self.sock.close()


def main():
    parser = argparse.ArgumentParser(prog='./gfx', add_help=False, usage='./gfx -p <port> [-h <hostname>]',
                                     description='Zappy graphical client')
    parser.add_argument('-p', type=int, required=True, metavar='port', help='server port')
    parser.add_argument('-h', dest='host', default='localhost', metavar='hostname',
                        help="name of the host, by default it'll be localhost")
    parser.add_argument('--help', action='help', help='show this help message and exit')
    args = parser.parse_args()
    try:
        gfx = ZappyGFX(args.host, args.p)
    except tk.TclError as exc:
        print(f'Error: cannot open the window: {exc}', file=sys.stderr)
        return 1
    gfx.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
