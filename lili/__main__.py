import ast
import curses
import curses.ascii
import io
import marshal
import opcode
import re
import sys
import types
import warnings
from typing import BinaryIO, TextIO, Union

from ike.bytecode import PartialCode, UnsafeBytecode

try:
    from importlib.util import MAGIC_NUMBER

except ImportError:
    pass

INSTR = re.compile(r"(\w+)(?:(\s+[@%])(\s*[^#]+)?)?(\s*#.*)?")
hasscope = opcode.hasname + opcode.haslocal + opcode.hasfree

warnings.simplefilter("always", UnsafeBytecode)


class Editor:
    def __init__(self, file: Union[BinaryIO, TextIO, str, None] = None):
        if file is None:
            self.filename = None
            self.src = [
                "LOAD_FAST @n",
                "LOAD_CONST @.5",
                "INPLACE_POWER",
                "RETURN_VALUE",
            ]
        elif isinstance(file, str):
            self.filename = file
            try:
                f = open(file, "rb")
            except FileNotFoundError:
                self.src = []
                return

            header = f.read(16)
            try:
                header.decode()

            except UnicodeDecodeError:
                if len(header) < 16:
                    raise EOFError
                if header[:4] != MAGIC_NUMBER:
                    raise RuntimeError(
                        "Can't open %s: wrong python version?" % file
                    )
                flags = int.from_bytes(header[4:8], "little")
                if flags & ~0b11:
                    raise RuntimeError(
                        "Can't open %s: unknown flags (%s)" % file, flags
                    )
                co = marshal.loads(f.read())
                if type(co) is not types.CodeType:  # noqa
                    raise RuntimeError("Can't open %s: whar")

                raise NotImplementedError
            else:
                self.src = (header + f.read()).decode().split("\n")
        else:
            raise NotImplementedError

        self.code = PartialCode([], [], [])
        self.cy = len(self.src) - 1
        self.cx = len(self.src[self.cy])
        self.lines = {}

    def run(self):
        curses.wrapper(self._main)

    def _main(self, screen: curses.window):
        self.screen = screen
        self.setup()
        self.on_resize(*screen.getmaxyx())
        while True:
            self.render()
            ch = screen.getch()
            if ch == curses.KEY_RESIZE:
                self.on_resize(*screen.getmaxyx())
            self.on_ch(ch)

    def setup(self):
        if curses.COLORS >= 256:
            colors = [
                (255, 233),  # Default
                (141, 233),  # Magenta (instruction)
                (182, 233),  # Grey-ish magenta (operator)
                (179, 233),  # Yellow (constant)
                (245, 233),  # Grey (comment)
                (237, 233),  # Darker grey (gutter)
                (232, 160),  # Black on red (error)
                (212, 233),  # Pink! :D (bytecode)
            ]
        else:
            colors = [
                (15, 0),
                (4, 0),
                (7, 0),
                (3, 0),
                (8, 0),
                (8, 0),
                (0, 9),
                (14, 0),
            ]
        for i, (f, b) in enumerate(colors):
            curses.init_pair(i + 1, f, b)

        self.screen.bkgd(" ", curses.color_pair(1))

    def render(self):
        screen = self.screen
        code = self.code
        editor = self.editor
        screen.erase()
        bc = 0
        code.bytecode.clear()
        code.constants.clear()
        code.names.clear()
        code.varnames.clear()
        code.freevars.clear()
        for i, ln in enumerate(self.src):
            fmt = INSTR.fullmatch(ln.rstrip())
            if fmt is None:
                screen.addstr(i, 0, "   ~", curses.color_pair(6))
                editor.addstr(i, 0, ln, curses.color_pair(5))
                continue

            x, o, v, c = fmt.groups()
            arg = None
            if not v or not o:
                arg = 0
            if op := opcode.opmap.get(x):
                if o and "%" in o:
                    try:
                        arg = int(v)
                    except (ValueError, TypeError):
                        pass
                elif o:
                    if op in opcode.hasconst:
                        try:
                            const = ast.literal_eval(v)
                        except (ValueError, SyntaxError):
                            pass
                        else:
                            for j, y in enumerate(code.constants):
                                if object.__eq__(y, const) is True:
                                    arg = j
                                    break
                            else:
                                arg = len(code.constants)
                                code.constants.append(const)
                    elif op in hasscope and v:
                        if op in opcode.hasname:
                            scope = code.names
                        elif op in opcode.haslocal:
                            scope = code.varnames
                        elif op in opcode.hasfree:
                            scope = code.freevars
                        name = v.strip()
                        if name not in scope:
                            scope.append(name)
                        arg = scope.index(name)
                    else:
                        try:
                            arg = int(v)
                        except (ValueError, TypeError):
                            pass

            editor.move(i, 0)
            editor.addstr(x, curses.color_pair(2))
            editor.addstr(o or "", curses.color_pair(3))
            editor.addstr(v or "", curses.color_pair(4))
            editor.addstr(c or "", curses.color_pair(5))
            if op is not None and arg is not None:
                code.bytecode.extend((op, arg,))
                screen.addstr(i, 0, format(bc, ">4"), curses.color_pair(6))
                editor.addstr(
                    i,
                    max(len(ln) + 2, 24),
                    f"{op:0>2x} {arg:0>2x}",
                    curses.color_pair(8)
                )
                self.lines[bc] = i
                bc += 2
            else:
                screen.addstr(i, 0, "   ?", curses.color_pair(6))
                editor.addstr(i, max(len(ln) + 2, 24), "?? ??", curses.color_pair(6))

        # with warnings.catch_warnings(record=True) as stinky:
        #     panel.move(1, 0)
        #     panel.addstr("co_stacksize", curses.color_pair(2))
        #     panel.addstr("=", curses.color_pair(3))
        #     panel.addstr(str(code.stacksize), curses.color_pair(4))
        #     panel.addstr(", ", curses.color_pair(3))
        #     try:
        #         for field, values in [
        #             ("co_consts", code.constants),
        #             ("co_names", code.names),
        #             ("co_varnames", code.varnames),
        #             ("co_freevars", code.freevars),
        #         ]:
        #             panel.addstr(field, curses.color_pair(2))
        #             panel.addstr("=(", curses.color_pair(3))
        #             for x in values[:16]:
        #                 r = repr(x)
        #                 if len(r) < 16:
        #                     panel.addstr(r, curses.color_pair(4))
        #                     panel.addstr(", ", curses.color_pair(3))
        #                 else:
        #                     panel.addstr(r[:16], curses.color_pair(4))
        #                     panel.addstr("..., ", curses.color_pair(3))
        #             panel.addstr("), ", curses.color_pair(3))
        #     except curses.error:
        #         pass

        #     for w in stinky:
        #         if isinstance(w.message, UnsafeBytecode):
        #             self.status.addstr(0, 0, str(w.message), curses.color_pair(7))
        #             i = self.lines[
        #                 int(str(w.message).split("[")[1].split("]")[0]) * 2
        #             ]
        #             ln = self.src[i]
        #             editor.chgat(i, 0, len(ln), curses.color_pair(7))
        #             break

        screen.move(self.cy, self.cx + 5)

    def on_resize(self, y, x):
        self.editor = self.screen.subwin(y - 1, x - 5, 0, 5)
        self.status = self.screen.subwin(1, x, y - 1, 0)

    def on_ch(self, ch: int):
        cx = self.cx
        cy = self.cy
        src = self.src
        if curses.ascii.isprint(ch):
            ln = self.src[cy]
            # If the cursor is inside the instruction name, auto-capitalise it.
            if cx == 0 or (m := INSTR.match(ln)) and cx-1 in range(*m.span(1)):
                src[cy] = ln[:cx] + chr(ch).upper() + ln[cx:]
            else:
                src[cy] = ln[:cx] + chr(ch) + ln[cx:]
            cx += 1
        elif ch == curses.KEY_BACKSPACE:
            if cx:
                src[cy] = src[cy][:cx-1] + src[cy][cx:]
                cx -= 1
            else:
                if not cy:
                    return
                cx = len(src[cy-1])
                src[cy-1] += src.pop(cy)
                cy -= 1
        elif ch == curses.ascii.NL:
            cy += 1
            cx = 0
            src.insert(cy, "")
        elif ch == curses.KEY_LEFT:
            if cx:
                cx -= 1
            elif cy:
                cy -= 1
                cx = len(src[cy])
        elif ch == curses.KEY_RIGHT:
            if cx < len(src[cy]):
                cx += 1
            elif cy < len(src)-1:
                cy += 1
                cx = 0
        elif ch == curses.KEY_UP:
            if cy:
                cy -= 1
                if cx > len(src[cy]):
                    cx = len(src[cy])
        elif ch == curses.KEY_DOWN:
            if cy < len(src)-1:
                cy += 1
                if cx > len(src[cy]):
                    cx = len(src[cy])

        self.cx = cx
        self.cy = cy


if __name__ == "__main__":
    if len(sys.argv) > 1:
        f = sys.argv[1]
    else:
        f = None
    Editor(f).run()
