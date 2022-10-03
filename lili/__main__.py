import ast
import curses
import curses.ascii
import getopt
# import inspect
import marshal
import opcode
import re
import sys
import tokenize
import types
import warnings
from collections import namedtuple
from curses import color_pair as color
from typing import BinaryIO, Optional, Union

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
    -c, --compile    Compile the input file, then edit it
    -f, --force      Open the file regardless of compatibility\
"""

warnings.simplefilter("always", UnsafeBytecode)


def read_pyc(f: BinaryIO, force: bool = False) -> tuple[bytes, types.CodeType]:
    f.seek(0)
    header = f.read(16)
    if len(header) < 16:
        raise EOFError
    if header[:4] != MAGIC_NUMBER and not force:
        raise RuntimeError(
            "Can't open %s: wrong python version?" % f.name
        )
    flags = int.from_bytes(header[4:8], "little")
    if flags & ~0b11 and not force:
        raise RuntimeError(
            "Can't open %s: unknown flags (%s)" % f.name, flags
        )
    co = marshal.loads(f.read())
    if type(co) is not types.CodeType:  # noqa
        raise RuntimeError("Can't open %s: whar")

    return header, co


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
            if type(x) is types.CodeType:  # noqa
                src.append("    %" + repr(marshal.dumps(x)))
            else:
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
        if op >= opcode.HAVE_ARGUMENT or arg:
            src.append(opcode.opname[op] + " %" + str(arg))
        else:
            src.append(opcode.opname[op])

    return src


def evaluate(n: Union[str, ast.AST]) -> object:
    """Like ast.literal_eval, but supporting all marshallable objects."""
    if isinstance(n, str):
        if n.startswith("%"):
            return "JO BIDEN !!"
        n = compile(n.lstrip(" \t"), "<lili>", "eval", ast.PyCF_ONLY_AST).body

    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Name):
        if n.id == "Ellipsis":
            return Ellipsis
        if n.id == "StopIteration":
            return StopIteration
    if seq := {ast.Tuple: tuple, ast.List: list, ast.Set: set}.get(type(n)):
        return seq(map(evaluate, n.elts))
    if isinstance(n, ast.Dict):
        return dict(zip(map(evaluate, n.keys), map(evaluate, n.values)))
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
        if isinstance(n.op, ast.USub):
            return -evaluate(n.operand)
        return +evaluate(n.operand)
    if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)):
        l, r = evaluate(n.left), evaluate(n.right)
        if isinstance(l, (int, float)) and isinstance(r, complex):
            if isinstance(n.op, ast.Add):
                return l + r
            return l - r
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
        if n.func.id == "set" and n.args == n.keywords == []:
            return set()
        if n.func.id == "frozenset" and n.keywords == []:
            if n.args == []:
                return frozenset()
            if len(n.args) == 1:
                return frozenset(evaluate(n.args[0]))

    raise ValueError


class Editor:
    def __init__(
        self,
        file: Optional[str] = None,
        needs_compile: bool = False,
        force: bool = False,
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
            self.format = "none"
        else:
            self.filename = file
            try:
                f = open(file, "rb")
            except FileNotFoundError:
                self.src = [""]
                self.code = PartialCode([], [], [])
                fmt = file.rsplit(".", 1)[1]
                if fmt in {"py", "pyc", "pyll"}:
                    self.format = fmt
                else:
                    self.format = "none"
            else:
                try:
                    self.code = PartialCode([], [], [])
                    content = f.read().decode()
                    if needs_compile:
                        self.src = unparse(compile(content, file, "exec"))
                        self.filename += "ll"
                        self.format = "pyll(generated)"
                    else:
                        self.src = content.split("\n")
                        self.format = "pyll"

                except UnicodeDecodeError:
                    try:
                        header, co = read_pyc(f, force=force)
                    except Exception as e:
                        raise e from None
                    self.code = PartialCode([], [], [])
                    self.src = unparse(co)
                    self.format = "pyc(MAGIC=%i%s:F=%x)" % (
                        int.from_bytes(header[:2], "little"),
                        "[HEXED!]" if header[:4] != MAGIC_NUMBER else "",
                        int.from_bytes(header[4:8], "little"),
                    )

        self.cy = 0
        self.cx = 0
        self.vy = 0
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
                                const = evaluate(val)
                            except Exception:
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
                                arg = evaluate(val)
                            except Exception:
                                pass
                    else:
                        try:
                            arg = evaluate(val)
                        except Exception:
                            pass
                else:
                    arg = 0

                if arg is not None and arg in range(256):
                    self.code.bytecode.extend((instr, arg))
                    self.lines[bc] = li + i
                    self.offsets[li + i] = bc
                    bc += 1

        if "CONSTS" in sections:
            for ln in sections["CONSTS"]:
                if ln.startswith("%"):
                    self.code.constants.append("JO BIDEN !!")
                else:
                    try:
                        const = evaluate(ln)
                    except Exception:
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

    def redraw(self):
        editor = self.editor
        editor.erase()

        for i, tokens in enumerate(self.parsed):
            offset = self.offsets.get(i)
            editor.move(i, 0)
            editor.clrtoeol()
            for t, v in tokens:
                colors = {
                    "name": 2, "op": 3, "const": 4, "section": 2, "field": 4
                }
                editor.addstr(v, color(colors.get(t, 5)))

            if offset is not None:
                instr, arg = raw = self.code.bytecode[offset*2:offset*2+2]
                editor.addstr("\t" + raw.hex(" "), color(8))
                if instr in opcode.hasjabs:
                    editor.addstr(self.lines[arg], 32, "<< %i" % offset)

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

        self.screen.bkgd(" ", color(1))
        curses.raw()
        curses.set_tabsize(24)

        self.editor = curses.newpad(
            max(len(self.src), 512),
            256
        )
        self.editor.bkgd(color(1))

    def render(self):
        editor = self.editor
        screen = self.screen
        status = self.status

        for i in range(self.my - 2):
            if (x := self.offsets.get(self.vy + i)) is not None:
                mark = format(x, ">4")
            else:
                if self.vy + i < len(self.src):
                    mark = "~"
                else:
                    mark = ""
            screen.addstr(i, 0, format(mark, ">4"), color(6))

        with warnings.catch_warnings(record=True) as stinky:
            status.addstr(0, 0, " " * self.mx, color(9))
            status.addstr(
                0, 1,
                (self.filename or "[scratch]") + ":" + self.format,
                color(9)
            )
            status.addstr(
                0, self.mx - 13, "Ctrl+g: help", color(9)
            )
            status.move(1, 0)
            status.addstr("stacksize", color(2))
            status.addstr("=", color(3))
            status.addstr(str(self.code.stacksize), color(4))
            status.addstr(", ", color(3))

            try:
                for field, values in [
                    ("consts", self.code.constants),
                    ("names", self.code.names),
                    ("varnames", self.code.varnames),
                    ("freevars", self.code.freevars),
                ]:
                    status.addstr(field, color(2))
                    status.addstr("=(", color(3))
                    for x in values:
                        status.addstr(repr(x), color(4))
                        status.addstr(", ", color(3))
                    status.addstr("), ", color(3))
            except curses.error:
                pass

            for w in stinky:
                if isinstance(w.message, UnsafeBytecode):
                    msg = str(w.message)
                    i = self.lines.get(
                        int(re.match(r".*: \[(\d+)", msg).group(1)),
                        0
                    )
                    ln = self.src[i]
                    editor.chgat(i, 0, len(ln), color(7))
                    break

            if stinky:
                counter = " ⏺ %s " % len(stinky)
                status.addstr(0, 0, " " * self.mx, color(7))
                status.addstr(0, 1, str(stinky[0].message), color(7))
                status.addstr(0, self.mx - len(counter) - 1, counter, color(7) | curses.A_REVERSE)

        screen.move(self.cy - self.vy, self.cx + 5)
        editor.noutrefresh(self.vy, 0, 0, 5, self.my - 2, self.mx - 1)
        status.noutrefresh()

    def on_resize(self, y, x):
        self.status = self.screen.subwin(2, x, y - 2, 0)
        self.my = y
        self.mx = x
        self.redraw()
        self.editor.noutrefresh(0, 0, 0, 5, y - 2, x - 1)
        self.screen.noutrefresh()

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
            self.redraw()
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
            self.redraw()
        elif ch == curses.ascii.NL:
            cy += 1
            cx = 0
            src.insert(cy, "")
            self.update_code()
            self.redraw()
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
        elif ch == curses.ascii.ctrl(ord("g")):
            self.screen.addstr(0, 0, "J")
            self.screen.getch()
        elif ch == curses.ascii.ctrl(ord("c")):
            raise SystemExit

        if cy - self.vy > self.my - 5:
            self.vy = cy - self.my + 5
        elif cy - self.vy < 2 and self.vy > 0:
            self.vy -= 1

        self.cx = cx
        self.cy = cy


if __name__ == "__main__":
    try:
        opts, args = getopt.getopt(
            sys.argv[1:], "hcf", ["help", "compile", "force"]
        )

    except getopt.GetoptError as e:
        print(USAGE)
        print()
        print(e, file=sys.stderr)

    else:
        needs_compile = False
        force = False
        for o, _ in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                break
            if o in ("-c", "--compile"):
                needs_compile = True
            if o in ("-f", "--force"):
                force = True
        else:
            f = None
            if args:
                f = args[0]
            Editor(f, needs_compile=needs_compile, force=force).run()
