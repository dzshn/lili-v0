import ast
import curses
import curses.ascii
import getopt
import marshal
import opcode
import re
import sys
import tokenize
import types
import warnings
from collections import namedtuple
from typing import BinaryIO, TextIO, Union, Optional

from ike.bytecode import PartialCode, UnsafeBytecode

try:
    from importlib.util import MAGIC_NUMBER

except ImportError:
    pass

Token = namedtuple("Token", "type value")
USAGE = """\
lili-editor 0.0.0
~Anya <me@dzshn.xyz>
Bytecode dissection and debugging.

Usage:
    lili [flags] [file]

Args:
    [file]    A plaintext file (actual script, or bytecode), or cached python
              bytecode (.pyc)

Flags:
    -h, --help       Print dis message
    -c, --compile    Compile the input file, then edit it\
"""

warnings.simplefilter("always", UnsafeBytecode)


def read_pyc(f: BinaryIO) -> types.CodeType:
    f.seek(0)
    header = f.read(16)
    if len(header) < 16:
        raise EOFError
    if header[:4] != MAGIC_NUMBER:
        raise RuntimeError(
            "Can't open %s: wrong python version?" % f.name
        )
    flags = int.from_bytes(header[4:8], "little")
    if flags & ~0b11:
        raise RuntimeError(
            "Can't open %s: unknown flags (%s)" % f.name, flags
        )
    co = marshal.loads(f.read())
    if type(co) is not types.CodeType:  # noqa
        raise RuntimeError("Can't open %s: whar")

    return co


def get_scope(code: PartialCode, op: int) -> Optional[list[object]]:
    if op in opcode.hasname:
        return code.names
    if op in opcode.haslocal:
        return code.varnames
    if op in opcode.hasfree:
        return code.freevars


def eq(a: object, b: object) -> bool:
    if a is b:
        return True
    if type(a) is type(b):
        try:
            return hash(a) == hash(b)
        except TypeError:
            pass
    return False


def idx(s: list, x: object) -> int:
    for i, y in enumerate(s):
        if eq(x, y):
            return i
    return -1


def unparse(co: types.CodeType) -> list[str]:
    src = []
    if co.co_consts:
        src.append("CONSTS:")
        for x in co.co_consts:
            src.append("    " + repr(x))
    for section, field in [
        ("NAMES", co.co_names),
        ("VARNAMES", co.co_varnames),
        ("FREEVARS", co.co_freevars),
        ("CELLVARS", co.co_cellvars),
    ]:
        if field:
            src.append(section + ":")
            for name in field:
                src.append("    " + name)
    src.append(f"FLAGS: {co.co_flags:#b}")
    src.append(f"STACKSIZE: {co.co_stacksize}")
    src.append("")
    for op, arg in zip(*[iter(co.co_code)] * 2):
        if op >= opcode.HAVE_ARGUMENT:
            src.append(opcode.opname[op] + " %" + str(arg))
        else:
            src.append(opcode.opname[op])

    return src


class Editor:
    def __init__(
        self,
        file: Union[BinaryIO, TextIO, str, None] = None,
        needs_compile: bool = False,
    ):
        if file is None:
            self.filename = None
            self.src = [
                "LOAD_FAST @n",
                "LOAD_CONST @.5",
                "INPLACE_POWER",
                "RETURN_VALUE",
            ]
            self.code = PartialCode([], [], [])
        elif isinstance(file, str):
            self.filename = file
            try:
                f = open(file, "rb")
            except FileNotFoundError:
                self.src = [""]
                self.code = PartialCode([], [], [])

            else:
                try:
                    self.code = PartialCode([], [], [])
                    content = f.read().decode()
                    if needs_compile:
                        self.src = unparse(compile(content, file, "exec"))
                        self.filename += "ll"
                    else:
                        self.src = content.split("\n")

                except UnicodeDecodeError:
                    try:
                        co = read_pyc(f)
                    except Exception as e:
                        raise e from None
                    self.code = PartialCode([], [], [])
                    self.src = unparse(co)
        else:
            raise NotImplementedError

        self.cy = len(self.src) - 1
        self.cx = len(self.src[self.cy])
        self.update_code()

    def parse(self):
        self.parsed = []
        body_pos = 0
        for i, ln in enumerate(self.src):
            if m := re.match(r"([A-Z_]+):(.*)?", ln):
                tokens = [Token("section", m.group(1)), Token("op", ":")]
                if m.group(2):
                    tokens.append(Token("field", m.group(2)))
                self.parsed.append(tokens)
                body_pos = i + 1
            elif body_pos and ln.startswith((" ", "\t")):
                self.parsed.append([Token("field", ln)])
                body_pos += 1
            else:
                break

        for ln in self.src[body_pos:]:
            tokens = []
            p = 0
            while p < len(ln):
                if ln[p] == "#":
                    tokens.append(Token("comment", ln[p:]))
                    break
                elif m := re.match(r"[A-Z_]+(?:\s+|\Z)", ln[p:]):
                    tokens.append(Token("name", m.group()))
                    p += m.end()
                elif m := re.match(r"@\s*", ln[p:]):
                    tokens.append(Token("op", m.group()))
                    p += m.end()
                    if m := re.match(tokenize.String, ln[p:]):
                        tokens.append(Token("const", m.group()))
                        p += m.end()
                    else:
                        m = re.match(".*(?=#)|.*", ln[p:])
                        tokens.append(Token("const", m.group()))
                        p += m.end()
                elif m := re.match(r"%\s*", ln[p:]):
                    tokens.append(Token("op", m.group()))
                    p += m.end()
                    if m := re.match(tokenize.Intnumber, ln[p:]):
                        tokens.append(Token("const", m.group()))
                        p += m.end()
                else:
                    tokens.append(Token("unknown", ln[p:]))
                    break

            self.parsed.append(tokens)

    def update_code(self):
        self.parse()
        sections = {}
        i = 0
        try:
            while True:
                tokens = self.parsed[i]
                if tokens[0].type != "section":
                    break
                sec = tokens[0].value.removesuffix(":")
                sections[sec] = []
                i += 1
                if len(tokens) > 2:
                    sections[sec].append(tokens[2].value.lstrip())
                    break
                while True:
                    tokens = self.parsed[i]
                    if tokens[0].type != "field":
                        break
                    sections[sec].append(tokens[0].value.lstrip())
                    i += 1

        except IndexError:
            pass

        self.code.constants.clear()
        self.code.names.clear()
        self.code.varnames.clear()
        self.code.freevars.clear()
        self.code.bytecode.clear()
        self.lines = {}
        self.offsets = {}
        bc = 0
        for li, tokens in enumerate(self.parsed[i:]):
            x = len(tokens)
            if x > 0 and tokens[0].type == "name":
                instr = opcode.opmap.get(tokens[0][1].strip())
                arg = None
                if instr is None:
                    continue
                if x >= 3 and tokens[2].type == "const":
                    _, (_, op), (_, val) = tokens[:3]  # meow
                    if op[0] == "@":
                        if instr in opcode.hasconst:
                            try:
                                const = ast.literal_eval(val)
                            except (SyntaxError, ValueError):
                                pass
                            else:
                                arg = idx(self.code.constants, const)
                                if arg == -1:
                                    arg = len(self.code.constants)
                                    self.code.constants.append(const)
                        elif (scope := get_scope(self.code, instr)) is not None:
                            name = val.strip()
                            if name not in scope:
                                scope.append(name)
                            arg = scope.index(name)
                        else:
                            try:
                                arg = ast.literal_eval(val)
                            except (ValueError, SyntaxError):
                                pass
                    else:
                        try:
                            arg = ast.literal_eval(val)
                        except (ValueError, SyntaxError):
                            pass
                else:
                    arg = 0

                if arg is not None and arg in range(256):
                    self.code.bytecode.extend((instr, arg))
                    self.lines[bc] = li + i
                    self.offsets[li + i] = bc
                    bc += 1

        if "CONSTANTS" in sections:
            for ln in sections["CONSTANTS"]:
                if ln.startswith("%"):
                    self.code.constants.append("JO BIDEN !!")
                else:
                    try:
                        const = ast.literal_eval(ln)
                    except (SyntaxError, ValueError):
                        pass
                    else:
                        if idx(self.code.constants, const) == -1:
                            self.code.constants.append(const)
        if "NAMES" in sections:
            for ln in sections["NAMES"]:
                self.code.names.append(ln.strip())
        if "VARNAMES" in sections:
            for ln in sections["VARNAMES"]:
                self.code.varnames.append(ln.strip())
        if "FREEVARS" in sections:
            for ln in sections["FREEVARS"]:
                self.code.freevars.append(ln.strip())


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
                (255, 233),  # [1] Default
                (141, 233),  # [2] Magenta (instruction)
                (182, 233),  # [3] Grey-ish magenta (operator)
                (179, 233),  # [4] Yellow (constant)
                (245, 233),  # [5] Grey (comment)
                (237, 233),  # [6] Darker grey (gutter)
                (232, 160),  # [7] Black on red (error)
                (212, 233),  # [8] Pink! :D (bytecode)
                (231, 235),  # [9] White on grey (status line)
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
                (8, 0),
            ]
        for i, (f, b) in enumerate(colors):
            curses.init_pair(i + 1, f, b)

        self.screen.bkgd(" ", curses.color_pair(1))
        curses.raw()
        curses.set_tabsize(24)

    def render(self):
        screen = self.screen
        code = self.code
        editor = self.editor
        screen.erase()

        self.parse()
        for i, tokens in enumerate(self.parsed):
            offset = self.offsets.get(i)
            editor.move(i, 0)
            for t, v in tokens:
                color = {"name": 2, "op": 3, "const": 4}.get(t, 5)
                editor.addstr(v, curses.color_pair(color))

            if offset is not None:
                screen.addstr(i, 0, format(offset, ">4"), curses.color_pair(6))
                editor.addstr(
                    "\t" + code.bytecode[offset*2:offset*2+2].hex(" "),
                    curses.color_pair(8),
                )
            else:
                screen.addstr(i, 0, "   ~", curses.color_pair(6))

        with warnings.catch_warnings(record=True) as stinky:
            self.status.addstr(0, 0, " " * self.mx, curses.color_pair(9))
            self.status.addstr(0, 1, self.filename or "[scratch]", curses.color_pair(9))
            self.status.move(1, 0)
            self.status.addstr("stacksize", curses.color_pair(2))
            self.status.addstr("=", curses.color_pair(3))
            self.status.addstr(str(self.code.stacksize), curses.color_pair(4))
            self.status.addstr(", ", curses.color_pair(3))

            try:
                for field, values in [
                    ("consts", code.constants),
                    ("names", code.names),
                    ("varnames", code.varnames),
                    ("freevars", code.freevars),
                ]:
                    self.status.addstr(field, curses.color_pair(2))
                    self.status.addstr("=(", curses.color_pair(3))
                    for x in values:
                        self.status.addstr(repr(x), curses.color_pair(4))
                        self.status.addstr(", ", curses.color_pair(3))
                    self.status.addstr("), ", curses.color_pair(3))
            except curses.error:
                pass

            for w in stinky:
                if isinstance(w.message, UnsafeBytecode):
                    msg = str(w.message)
                    self.status.addstr(0, 0, " " * self.mx, curses.color_pair(7))
                    self.status.addstr(0, 0, msg, curses.color_pair(7))
                    i = self.lines.get(
                        int(re.match(r".*: \[(\d+)", msg).group(1)),
                        0
                    )
                    ln = self.src[i]
                    editor.chgat(i, 0, len(ln), curses.color_pair(7))
                    break

        screen.move(self.cy, self.cx + 5)

    def on_resize(self, y, x):
        self.editor = self.screen.subwin(y - 2, x - 5, 0, 5)
        self.status = self.screen.subwin(2, x, y - 2, 0)
        self.my = y
        self.mx = x

    def on_ch(self, ch: int):
        cx = self.cx
        cy = self.cy
        src = self.src
        if curses.ascii.isprint(ch):
            ln = self.src[cy]
            # If the cursor is inside the instruction name, auto-capitalise it.
            if cx == 0 or re.match(r"[A-Z_]+", ln[cx-1:]):
                src[cy] = ln[:cx] + chr(ch).upper() + ln[cx:]
            else:
                src[cy] = ln[:cx] + chr(ch) + ln[cx:]
            cx += 1
            self.update_code()
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
            self.update_code()
        elif ch == curses.ascii.NL:
            cy += 1
            cx = 0
            src.insert(cy, "")
            self.update_code()
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
        elif ch == curses.ascii.ctrl(ord("c")):
            raise SystemExit

        self.cx = cx
        self.cy = cy


if __name__ == "__main__":
    try:
        opts, args = getopt.getopt(sys.argv[1:], "hc", ["help", "compile"])

    except getopt.GetoptError as e:
        print(USAGE)
        print()
        print(e, file=sys.stderr)

    else:
        needs_compile = False
        for o, _ in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                break
            if o in ("-c", "--compile"):
                needs_compile = True
        else:
            f = None
            if args:
                f = args[0]
            Editor(f, needs_compile=needs_compile).run()
