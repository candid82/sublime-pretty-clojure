from __future__ import print_function

import os
import platform
import re
import subprocess
import traceback

import sublime
import sublime_plugin

ERROR_TEMPLATE = """
<div><b>{row}:</b> {text}</div>
"""

is_windows = platform.system() == 'Windows'
startup_info = None
if is_windows:
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

settings = None
view_errors = {}


def plugin_loaded():
    global settings
    settings = sublime.load_settings('Cljpprint.sublime-settings')


class Command(object):

    def __init__(self, cmd, view, window):
        self.view = view
        self.window = window
        self.cmd = cmd

    def run(self, stdin):
        proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            stdout=subprocess.PIPE, startupinfo=startup_info)
        if isinstance(stdin, str):
            stdin = stdin.encode()
        stdout, stderr = proc.communicate(stdin)
        return stdout, stderr, proc.returncode


class Error(object):

    # <stdin>:125:32: Read error: Map literal must contain an even number of forms

    line_re = re.compile(r'<stdin>:(\d+):(\d+):\s+(.*)\Z')

    def __init__(self, text, region, row, col, filename):
        self.text = text
        self.region = region
        self.row = row
        self.col = col
        self.filename = filename

    @classmethod
    def parse_stderr(cls, stderr, region, view):
        errors = []
        region_row, region_col = view.rowcol(region.begin())
        if not isinstance(stderr, str):
            stderr = stderr.decode('utf-8')
        fn = os.path.basename(view.file_name())
        stderr = stderr.replace('<standard input>', fn)
        for raw_text in stderr.splitlines():
            print(raw_text)
            match = cls.line_re.match(raw_text)
            if not match:
                continue
            row = int(match.group(1)) - 1
            col = int(match.group(2)) - 1
            text = match.group(3)
            if row == 0:
                col += region_col
            row += region_row
            a = view.text_point(row, col)
            b = view.line(a).end()
            errors.append(Error(text, sublime.Region(a, b), row, col, fn))
        return errors


class FormatterError(Exception):

    def __init__(self, errors):
        super(FormatterError, self).__init__('error running formatter')
        self.errors = errors


class Formatter(object):

    def __init__(self, view):
        self.view = view
        self.encoding = self.view.encoding()
        if self.encoding == 'Undefined':
            self.encoding = 'utf-8'
        self.window = view.window()
        cmds = settings.get('cmds', ["joker", "--format", "-"]) or []
        self.cmds = [Command(cmd, self.view, self.window) for cmd in cmds]

    def format(self, region):
        self._clear_errors()
        code = self.view.substr(region)
        for cmd in self.cmds:
            code, stderr, return_code = cmd.run(code)
            if stderr or return_code != 0:
                errors = Error.parse_stderr(stderr, region, self.view)
                self._show_errors(errors, return_code, cmd)
                raise FormatterError(errors)
        self._hide_error_panel()
        return code.decode(self.encoding)

    def _clear_errors(self):
        self.view.set_status('cljpprint', '')
        self.view.erase_regions('cljpprint')

    def _hide_error_panel(self):
        self.window.run_command('hide_panel', {'panel': 'output.cljpprint'})

    def _show_errors(self, errors, return_code, cmd):
        self.view.set_status('cljpprint', '{} failed with return code {}'.format(
            cmd.cmd[0], return_code))
        self._show_error_panel(errors)
        self._show_error_regions(errors)

    def _show_error_regions(self, errors):
        self.view.add_regions(
            'cljpprint', [e.region for e in errors], 'invalid.illegal', 'dot',
            (sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE |
             sublime.DRAW_SQUIGGLY_UNDERLINE))

    def _show_error_panel(self, errors):
        characters = '\n'.join([e.text for e in errors])
        p = self.window.create_output_panel('cljpprint')
        p.set_scratch(True)
        p.run_command('select_all')
        p.run_command('right_delete')
        p.run_command('insert', {'characters': characters})


def is_clojure_source(view):
    return view.score_selector(0, 'source.clojure') != 0 or view.score_selector(0, 'source.edn') != 0


def run_formatter(edit, view, regions):
    global view_errors
    if view.id() in view_errors:
        del view_errors[view.id()]
    try:
        formatter = Formatter(view)
        for region in regions:
            view.replace(edit, region, formatter.format(region))
    except FormatterError as e:
        view_errors[view.id()] = e.errors
    except Exception:
        sublime.error_message(traceback.format_exc())


class CljpprintCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        if not is_clojure_source(self.view):
            return

        # Format selected text, or the whole buffer in case no text is selected,
        # or if 'format_selected' option is set to false (default)
        regions = None
        if settings.get('format_selected', False):
            regions = list(filter(lambda r: r.begin() != r.end(), self.view.sel()))

        regions = regions if regions else [sublime.Region(0, self.view.size())]
        run_formatter(edit, self.view, regions)


class CljPpprintListener(sublime_plugin.EventListener):

    def _show_errors_for_row(self, view, row, point):
        if not is_clojure_source(view):
            return
        errors = view_errors.get(view.id())
        if not errors:
            return
        row_errors = [e for e in errors if e.row == row]
        if not row_errors:
            return
        html = '\n'.join([ERROR_TEMPLATE.format(row=e.row + 1, text=e.text)
                          for e in row_errors])
        view.show_popup(html, flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                        location=point, max_width=600)

    def on_hover(self, view, point, hover_zone):
        if hover_zone != sublime.HOVER_TEXT:
            return
        row, _ = view.rowcol(point)
        self._show_errors_for_row(view, row, point)

    def on_pre_save(self, view):
        if not settings.get('format_on_save', True):
            return
        view.run_command('cljpprint')
