from typing import cast
from Default.exec import ExecCommand  # type: ignore
from glob import iglob
from os import makedirs
from os.path import isfile
from os.path import join
from os.path import realpath
from pathlib import Path
import json
import os
import shlex
import sublime
import sublime_plugin
import subprocess
import threading
import time
import sys
import re

# Add plugin directory to path, so side-by-side modules succeeds
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

import cmakepresets
import jsonschema

from typing import Dict, List, Union, Optional, Any, Callable, Tuple


QUERY = {
    "requests": [
        {"kind": "codemodel",  "version": 2},
    ]
}  # type: Dict[str, Any]


CLIENT_STR = "client-sublimetext"


class CheckOutputException(Exception):
    """Gets raised when there's a non-empty error stream."""
    def __init__(self, errs):
        super(CheckOutputException, self).__init__()
        self.errs = errs

    def __str__(self) -> str:
        return self.errs


def check_output(shell_cmd, env=None, cwd=None):
    startupinfo = None
    if sublime.platform() == "linux":
        cmd = ["/bin/bash", "-c", shell_cmd]
        shell = False
    elif sublime.platform() == "osx":
        cmd = ["/bin/bash", "-l", "-c", shell_cmd]
        shell = False
    else:  # sublime.platform() == "windows"
        cmd = shell_cmd
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        shell = True
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=startupinfo,
        shell=shell,
        cwd=cwd)
    outs, errs = proc.communicate()

    encoding = "utf-8"
    errs = errs.decode(encoding)
    if errs:
        raise CheckOutputException(errs)
    return outs.decode(encoding)


def get_vcvarsall_path(desired_vs_major_version: int) -> str:
    if desired_vs_major_version < 15:
        raise ValueError("major versions less than 15 (2017) are not supported")
    for vs in get_all_vs_installed_versions():
        path = vs["path"]
        version = vs["version"]
        major_version = int(version.split(".")[0])
        if major_version == desired_vs_major_version:
            return join(path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
    raise RuntimeError(
        " ".join((
            "cannot find a visual studio SDK for major version",
            " {}"
        )).format(desired_vs_major_version)
    )


def parse_vcvarsall(vcvarsall_path: str,
                    target_architecture: str,
                    host_architecture: str) -> 'Dict[str, str]':
    if host_architecture == target_architecture:
        arg = host_architecture
    else:
        arg = "{}_{}".format(host_architecture, target_architecture)
    env_cmd = '"{}" {}'.format(vcvarsall_path, arg)
    shell = os.environ["COMSPEC"]

    cmd = '{} /s /c "{} & set"'.format(shell, env_cmd)
    out = check_output(cmd)

    result = {}
    for line in out.split("\n"):
        if '=' not in line:
            continue
        line = line.strip()
        key, value = line.split('=', 1)
        if key.lower() in ("include", "lib", "libpath", "path"):
            if value.endswith(os.pathsep):
                value = value[:-1]
            # Cmake freaks out if path if not upper case
            result[key.upper()] = value
    return result


def get_vs_env(desired_vs_major_version: int,
               host_architecture: str,
               target_architecture: str) -> 'Dict[str, str]':
    return parse_vcvarsall(
        get_vcvarsall_path(desired_vs_major_version),
        host_architecture,
        target_architecture)


def get_vs_env_from_generator_str(
    generator_str: str,
    host_architecture: str,
    target_architecture: str
) -> 'Dict[str, str]':
    return get_vs_env(
        get_vs_major_version_from_generator_str(generator_str),
        host_architecture,
        target_architecture)


def get_vs_major_version_from_generator_str(generator_str: str) -> int:
    if generator_str == "Ninja":
        generator_str = get_default_vs_generator_name()
    words = generator_str.split()
    if len(words) > 2:
        return int(words[2])
    raise RuntimeError("unexpected generator string: {}".format(generator_str))


def cmake_arch_to_vs_arch(arch: str) -> str:
    if arch == "x64":
        return "amd64"
    elif arch == "x86":
        return "x86"
    elif arch == "arm":
        return "arm"
    raise ValueError("unknown platform/toolset architecture: {}".format(arch))


def get_all_vs_generator_names():
    result = []
    for gen in cast(dict, capabilities("generators")):
        name = gen["name"]
        if name.startswith("Visual Studio"):
            if not name.endswith("Win64") and not name.endswith("ARM"):
                result.append(name)
    return result

def get_all_vs_installed_versions():
    cwd = join(os.environ["PROGRAMFILES(X86)"], "Microsoft Visual Studio",
               "Installer")
    cmd = "vswhere.exe -prerelease -legacy -format json -utf8"
    res = check_output(cmd, cwd=cwd)
    data = json.loads(res)
    return [{"path": vs["installationPath"],
             "version": vs["installationVersion"]} for vs in data]


def get_default_vs_generator_name() -> str:
    names = get_all_vs_generator_names()
    f = get_vs_major_version_from_generator_str
    versions = [f(n) for n in names]
    installed = get_all_vs_installed_versions()
    for ver, name in sorted(zip(versions, names), reverse=True):
        for installation in installed:
            if installation["version"].startswith(str(ver)):
                return name
    raise RuntimeError("unable to find default MSVC generator name")


__capabilities: Optional[Dict[str, Any]] = None
__capabilities_cv = threading.Condition()


def plugin_loaded() -> None:
    settings = sublime.load_settings("CMakeBuilder.sublime-settings")
    settings.add_on_change("CMakeBuilder", __reload_capabilities)
    __reload_capabilities()

def plugin_unloaded() -> None:
    with __capabilities_cv:
        __capabilities = None

def __reload_capabilities() -> None:
    sublime.set_timeout_async(__reload_capabilities_async)

def __reload_capabilities_async() -> None:
    global __capabilities
    settings = sublime.load_settings("CMakeBuilder.sublime-settings")
    cmake = settings.get("cmake_binary", "cmake")
    try:
        command = "{} -E capabilities".format(cmake)
        log("running", command)
        capabilities = json.loads(check_output(command))
        with __capabilities_cv:
            __capabilities = capabilities
    except Exception as e:
        msg = ["There was an error loading cmake's capabilities.",
            "Your \"cmake_binary\" setting is set to \"{}\".".format(cmake),
            "Please make sure that this points to a valid cmake executable."]
        if sublime.platform() == "osx":
            msg.append("Alternatively, it is possible that your ~/.bash_profile is broken.")
        sublime.error_message(" ".join(msg))
        log(str(e))
        __capabilities = {"error": None}


def capabilities(key) -> Union[None, List[str], str, Dict[str, str]]:
    global __capabilities
    with __capabilities_cv:
        __capabilities_cv.wait_for(lambda: __capabilities is not None)
        assert __capabilities is not None
        if "error" in __capabilities:
            raise ValueError("Error loading capabilities")
        else:
            return __capabilities.get(key, None)


class Generator:

    def syntax(self) -> str:
        raise NotImplementedError()

    def regex(self) -> str:
        raise NotImplementedError()


class NinjaGenerator(Generator):

    def syntax(self) -> str:
        if sublime.platform() == "windows":
            return syntax("Ninja+CL")
        else:
            return syntax("Ninja")

    def regex(self) -> str:
        if sublime.platform() == "windows":
            return r'^(.+)\((\d+)\):() (.+)$'
        else:
            return r'(.+[^:]):(\d+):(\d+): (?:fatal )?((?:error|warning): .+)$'


class UnixMakefilesGenerator(Generator):

    def syntax(self) -> str:
        return syntax("Make")

    def regex(self) -> str:
        return r'(.+[^:]):(\d+):(\d+): (?:fatal )?((?:error|warning): .+)$'


class NMakeMakefilesGenerator(Generator):

    def syntax(self) -> str:
        return syntax("Make")

    def file_regex(self) -> str:
        return r'^(.+)\((\d+)\):() (.+)$'

class VisualStudioGenerator(Generator):

    def syntax(self) -> str:
        return syntax("Visual_Studio")

    def regex(self) -> str:
        return r'^  (.+)\((\d+)\)(): ((?:fatal )?(?:error|warning) \w+\d\d\d\d: .*) \[.*$'


def make_generator(build_folder: str, generator: Optional[str]) -> Generator:
    if generator is None:
        generator = load_reply(build_folder)["cmake"]["generator"]["name"]
    elif generator == "Ninja":
        return NinjaGenerator()
    elif generator == "NMake Makefiles":
        return NMakeMakefilesGenerator()
    elif generator.startswith("Visual Studio"):
        return VisualStudioGenerator()
    elif generator == "Unix Makefiles":
        return UnixMakefilesGenerator()
    raise KeyError("unknown generator")

def file_api(build_folder: str) -> str:
    return join(build_folder, ".cmake", "api", "v1")


def file_api_query(build_folder: str) -> str:
    return join(file_api(build_folder), "query", CLIENT_STR)


def file_api_reply(build_folder: str) -> str:
    return join(file_api(build_folder), "reply")


def ensure_query_path_exists(build_folder: str) -> None:
    makedirs(file_api_query(build_folder), exist_ok=True)


def expand(window: sublime.Window, d) -> 'Any':
    return sublime.expand_variables(d, window.extract_variables())


def write_query(window: sublime.Window, build_folder: str) -> None:
    ensure_query_path_exists(build_folder)
    with open(join(file_api_query(build_folder), "query.json"), "w") as fp:
        json.dump(QUERY, fp, check_circular=False)


def get_index_file(build_folder: str) -> str:
    path = join(file_api_reply(build_folder), "index-")
    # Whenever a new index file is generated it is given a new name and any old
    # one is deleted. During the short time between these steps there may be
    # multiple index files present; the one with the largest name in
    # lexicographic order is the current index file.
    return sorted(iglob(path + "*.json"), reverse=True)[0]


def load_reply(build_folder: str) -> dict:
    with open(get_index_file(build_folder), "r") as fp:
        return json.load(fp)

def  get_default_cmake_generator(view: sublime.View, cmake: dict) -> Optional[str]:
    key = "default_{}_generator".format(sublime.platform())
    default_gen = get_setting(view, key)
    if default_gen is None:
        if sublime.platform() == "windows":
            default_gen = get_default_vs_generator_name()
    return default_gen


def get_cmake_generator(view: sublime.View, cmake: dict) -> Optional[str]:
    default_gen = get_default_cmake_generator(view, cmake)
    return get_cmake_value(cmake, 'generator', default_gen)

def get_target_arch_from_architecture(platform: Optional[str]) -> Optional[str]:
    if not platform:
        return None
    first = platform.split(",")[0]
    if '=' in first:
        return None
    arch = first.strip().lower()

    # NOTE: VS supports x64, ARM64 and Win32
    if arch == "win32":
        arch = "x86"
    elif arch == "arm64":
        arch = "arm"

    return arch

def get_host_arch_from_toolset(toolset: Optional[str]) -> Optional[str]:
    if not toolset:
        return None
    for p in toolset.split(','):
        k, _, v = p.partition('p')
        if key.strip() == "host":
            return value.strip()
    return None

def get_setting(view: Optional[sublime.View], key, default=None) -> Union[bool, str]:
    if view:
        settings = view.settings()
        if settings.has(key):
            return settings.get(key)
    settings = sublime.load_settings('CMakeBuilder.sublime-settings')
    return settings.get(key, default)


def get_cmake_binary() -> str:
    return str(get_setting(None, "cmake_binary", "cmake"))


def get_ctest_binary() -> str:
    return str(get_setting(None, "ctest_binary", "ctest"))


def log(*args) -> None:
    if get_setting(None, "cmake_debug", False):
        print("CMakeBuilder:", *args)

def warn(*args) -> None:
    print("CMakeBuilder:", *args)


def syntax(name: str) -> str:
    return "Packages/CMakeBuilder/Syntax/{}.sublime-syntax".format(name)


def get_cmake_value(
    the_dict: 'Dict[str, Any]',
    key: str,
    default=None
) -> 'Any':
    try:
        return the_dict[sublime.platform()][key]
    except KeyError:
        pass
    try:
        return the_dict[key]
    except KeyError:
        return default


def get_cmake_env(window: sublime.Window) -> Dict[str, str]:
    try:
        data = window.project_data()
        if isinstance(data, dict):
            cmake = data["settings"]["cmake"]
            return get_cmake_value(cmake, "env", {})
    except Exception:
        pass
    return {}



class CmakeBuildPresetCommand(ExecCommand):

    def run(self,
            working_dir: str,
            build_dir: str,
            preset: str,
            env: 'Dict[str, str]',
            generator: 'Optional[str]') -> None:
        gen = make_generator(working_dir, generator)
        build_dir = sublime.expand_variables(build_dir, self.window.extract_variables())
        cmd = [get_cmake_binary(), "--build", build_dir, "--preset", preset]
        super().run(cmd=cmd,
                    working_dir=working_dir,
                    env=env,
                    syntax=gen.syntax(),
                    file_regex=gen.regex())

class CmakeBuildCommand(ExecCommand):

    def run(
        self,
        working_dir: str,
        build_dir: str,
        config: str,
        env: 'Dict[str, str]',
        build_target: 'Optional[str]' = None,
        generator: 'Optional[str]' = None,
    ) -> None:
        gen = make_generator(working_dir, generator)
        build_dir = sublime.expand_variables(build_dir, self.window.extract_variables())
        cmd = [get_cmake_binary(), "--build", build_dir, "--config", config]
        if build_target:
            cmd.extend(["--target", build_target])
        super().run(
            cmd=cmd,
            working_dir=working_dir,
            env=env,
            syntax=gen.syntax(),
            file_regex=gen.regex())


cached_command_line_args = ""


class CommandLineArgumentsInputHandler(sublime_plugin.TextInputHandler):

    @classmethod
    def initial_text(cls) -> str:
        return cached_command_line_args

    def confirm(self, text: str) -> None:
        global cached_command_line_args
        cached_command_line_args = text


class CmakeRunCommand(sublime_plugin.WindowCommand):

    def on_done(self, command_line_args: str) -> None:
        global cached_command_line_args
        cached_command_line_args = command_line_args
        if sublime.platform() == "windows":
            posix = False
            shell = ["cmd.exe", "/C"]
            executable = ".\\{}".format(self.artifact.replace("/", "\\"))
            conjunction = "&"
            debugger = []  # type: List[str]
            if self.debug:
                sublime.error_message(
                    " ".join((
                        "There is no support for WinDbg.exe, because I have not ",
                        "found a need for it. If you want to use WinDbg.exe, ",
                        "consider contributing on github.com/rwols/CMakeBuilder",
                    ))
                )
                return
        else:
            posix = True
            shell = ["/bin/bash", "-c"]
            executable = "./{}".format(self.artifact)
            conjunction = "&&"
            if sublime.platform() == "linux":
                debugger = ["gdb", "-q", "--args"] if self.debug else []
            else: # osx
                debugger = ["lldb", "--"] if self.debug else []
        view = self.window.active_view()
        cmd = [get_cmake_binary(), "--build", ".", "--config", self.config,
               "--target", self.build_target, conjunction]
        cmd.extend(debugger)
        cmd.append(executable)
        cmd.extend(shlex.split(command_line_args, posix=posix))
        cmd = shell + [" ".join(cmd)]
        args = {
            "title": self.build_target,
            "env": self.env,
            "cmd": cmd,
            "cwd": self.working_dir,
            "auto_close": get_setting(view, "terminus_auto_close", False)}
        if get_setting(view, "terminus_use_panel", False):
            args["panel_name"] = self.build_target
        self.window.run_command("terminus_open", args)

    def run(
        self,
        working_dir: str,
        config: str,
        env: 'Dict[str, str]',
        build_target: str,
        artifact: str,
        generator: 'Optional[str]' = None,
        debug=False,
    ) -> None:
        self.working_dir = working_dir
        self.config = config
        self.env = env
        self.build_target = build_target
        self.artifact = artifact
        self.generator = generator
        self.debug = debug
        self.window.show_input_panel("Command Line Arguments: ",
                                     cached_command_line_args, self.on_done,
                                     None, None)


class CtestRunCommand(ExecCommand):
    def run(
        self,
        env: 'Dict[str, str]',
        working_dir: str,
        config: str,
        generator: 'Optional[str]' = None,
    ) -> None:
        extra_args = get_setting(self.window.active_view(),
                                 "ctest_command_line_args", "")
        super().run(
            cmd=[get_ctest_binary(), "-C", config] + [str(extra_args)],
            working_dir=working_dir,
            env=env,
            syntax=syntax("CTest"))


class CmakeInfo:

    __slots__ = ("unexpanded_root_folder", "build_presets", "view", "host_arch", "target_arch", "unexpanded_build_folder", "build_folder", "overrides",
                 "generator", "platform", "toolset", "env", "vs_major_version",
                 "root_folder", "configure_presets", "presets", "preset", "has_presets", "presets_path", "__data", "window")

    def __init__(self, window: sublime.Window) -> None:
        self.window = window
        default_build_folder = get_setting(window.active_view(), "default_build_folder",
                              "$folder/build")
        default_root_folder = get_setting(window.active_view(), "default_root_folder",
                                          "$folder")
        try:
            # Load settings from project
            data = window.project_data()
            if data is not None:
                self.__data = data["settings"]["cmake"]
                if "root_folder" not in self.__data:
                    self.__data["root_folder"] = default_root_folder
                if "build_folder" not in self.__data:
                    self.__data["build_folder"] = default_build_folder
        except Exception:
            # Use defaults
            self.__data = {
                "build_folder": default_build_folder,
                "root_folder": default_root_folder,
            }

        self.unexpanded_build_folder = self.__get_val("build_folder")  # type: str
        self.unexpanded_root_folder = self.__get_val("root_folder", "$folder") # type: str

        self.__data = expand(window, self.__data)

        self.build_folder = self.__get_val("build_folder")  # type: str
        self.root_folder = self.__get_val("root_folder")  # type: str

        if self.root_folder:
            self.root_folder = realpath(self.root_folder)

        self.presets_path = join(self.root_folder, "CMakePresets.json")
        self.has_presets = self.__has_presets()
        if not isfile(join(self.root_folder, "CMakeLists.txt")):
            raise FileNotFoundError()

    def __get_val(self, key: str, default: 'Any' = None) -> 'Any':
        return get_cmake_value(self.__data, key, default)

    def __on_select_preset(self, presets, callback):
        # Default on select preset callback
        # Shows the available presets and prompts for user input

        preset_names = [["None", "Don't use a preset"]] + [[p.get("displayName", p["name"]), p.get("description", "")] for p in presets]

        def on_selected(idx):
            if idx >= 1:
                callback(presets[idx - 1])
            elif idx == 0:
                callback(None)

        self.window.show_quick_panel(preset_names, on_selected, placeholder = "Select a CMake preset")

    def load(self, load_done_cb = None, select_preset_cb = None) -> None:
        if not select_preset_cb:
            select_preset_cb = self.__on_select_preset

        self.overrides = self.__get_val("command_line_overrides", {})  # type: Dict[str, str]

        self.view = self.window.active_view()
        if not self.view:
            raise RuntimeError("missing view")

        self.generator = self.__get_val('generator')
        self.platform = self.__get_val("platform")  # type: Optional[str]
        self.toolset = self.__get_val("toolset")  # type: Dict[str, str]
        self.host_arch = None # type: Optional[str]
        self.target_arch = None # type: Optional[str]
        self.vs_major_version = self.__get_val("vs_major_version")  # type: int
        if not self.vs_major_version:
            versions = self.__get_val("visual_studio_versions", [])  # type: List[int]
            if versions:
                self.vs_major_version = versions[0]
        self.env = self.__get_val("env", {})  # type: Dict[str, str]
        
        self.configure_presets = []
        self.build_presets = []
        self.presets = None # type: Optional[cmakepresets.CMakePresets]
        self.preset = None
        self.__load_presets()
        
        if self.configure_presets:
            select_preset_cb(self.configure_presets, lambda preset: self.__on_preset_selected(preset, load_done_cb))
        else:
            self.__on_preset_selected(load_done_cb=load_done_cb)

    def __on_preset_selected(self, preset = None, load_done_cb = None):
        self.preset = preset

        if self.preset:
            self.__load_build_presets()

            # NOTE: If generator not set explicitly in cmake settings, use generator
            #       from preset or the default generator
            if not self.generator:
                self.generator = self.preset.get("generator", get_default_cmake_generator(self.view, self.__data))

            # if self.build_presets:

            binary_dir = self.preset.get("binaryDir", None)
            if binary_dir:
                # NOTE: We have build presets, and configure preset has a 
                #       binary dir set. We must use the configured binary dir,
                #       otherwise CMake will not find the build files when building
                #       with a build preset
                target = Path(binary_dir).resolve()
                base = Path(self.root_folder).resolve()

                binary_dir = str((base/target).resolve().relative_to(base))

                # Check for explicitly defined build_folder in cmake settings
                try:
                    cmake_build_folder = get_cmake_value(self.window.project_data()["settings"]["cmake"], "build_folder", None)
                except:
                    cmake_build_folder = None

                # If explicitly defined in cmake settings *and* configure preset *and* there are build presets, error
                # NOTE: When using build presets, cmake always expects build files to be in the directory specified in the configure preset 
                if cmake_build_folder and self.build_presets:
                    # TODO: Raise error or just use preset build folder?
                    raise RuntimeError("Build folder specified in CMake settings, but CMake preset has build folder which must be used")

                if not cmake_build_folder:
                    self.build_folder = binary_dir
                    self.unexpanded_build_folder = binary_dir

        if sublime.platform() == "windows":
            self.__update_windows_environment(self.__data)

        if load_done_cb:
            load_done_cb()

    def get_configure_presets(self) -> 'List[Dict]':
        return self.configure_presets

    def get_build_presets(self) -> 'List[Dict]':
        return self.build_presets 

    def to_command(self) -> 'List[str]':
        cmd = [get_cmake_binary(), ".", "-B", self.build_folder]
        if self.generator:
            cmd.extend(["-G", self.generator])
        if self.platform:
            cmd.extend(["-A", self.platform])
        if self.toolset:
            cmd.append(self.__convert_toolset_to_str())
        if self.overrides:
            cmd.extend(self.__convert_overrides_to_list())
        if self.preset:
            cmd.extend(["--preset", self.preset["name"]])
        return cmd

    def __str__(self) -> str:
        return " ".join(self.to_command())

    def __convert_toolset_to_str(self) -> str:
        items = ["{}={}".format(*kv) for kv in self.toolset.items()]
        return "-T{}".format(",".join(items))

    def __convert_overrides_to_list(self) -> 'List[str]':
        result = []  # type: List[str]
        for k, val in self.overrides.items():
            try:
                if isinstance(val, bool):
                    v = "ON" if val else "OFF"
                else:
                    v = str(val)
                result.append("-D")
                result.append("{}={}".format(k, v))
            except AttributeError as e:
                pass
            except ValueError as e:
                pass
        return result

    def __update_windows_environment(self, data: 'Dict[str, Any]') -> None:
        if not self.generator:
            self.generator = get_default_vs_generator_name()

        host_arch = None
        if self.preset:
            toolset = self.preset.get("toolset", None)
            if isinstance(toolset, dict):
                toolset = toolset.get("value", None)
            host_arch = get_host_arch_from_toolset(toolset)

        if self.toolset:
            arch = self.toolset.get("host", None)
            if arch:
                host_arch = arch
        if not host_arch:
            host_arch = sublime.arch()


        target_arch = None
        if self.preset:
            architecture = self.preset.get("architecture")
            if isinstance(architecture, dict):
                architecture = architecture.get("value", None)
            target_arch = get_target_arch_from_architecture(architecture)

        if self.platform:
            # TODO: In settings, platform should be dict instead of string, 
            #       platform may include other options besides architecture,
            #       such as SDK version to use 
            target_arch = self.platform
        old_target_arch = get_cmake_value(data, "target_architecture")
        if old_target_arch:
            # If old style target arch set in cmake settings, use that
            if old_target_arch == "amd64":
                target_arch = "x64"
            else:
                target_arch = old_target_arch

        if not target_arch:
            # NOTE: If target arch still not set, use x64
            target_arch = "x64"

        host_arch = cmake_arch_to_vs_arch(host_arch)
        target_arch = cmake_arch_to_vs_arch(target_arch)
        if self.vs_major_version:
            env = get_vs_env(self.vs_major_version, host_arch, target_arch)
        else:
            assert self.generator
            env = get_vs_env_from_generator_str(self.generator, host_arch,
                                                target_arch)
        self.env.update(env)

    def __has_presets(self) -> bool:
        return isfile(self.presets_path)

    def __evaluate_condition(self, cond) -> bool:
        if cond is True or cond is False:
            return cond

        if cond is None:
            return True

        cond_type = cond["type"]

        if cond_type == "const":
            return bool(cond["value"])

        if cond_type in ("equals", "notEquals"):
            lhs = cond["lhs"]
            rhs = cond["rhs"]
            res = lhs == rhs
            return res if cond_type == "equals" else not res

        if cond_type in ("inList", "notInList"):
            string = cond["string"]
            lst = cond["list"]
            res = string in lst
            return res if cond_type == "inList" else not res

        if cond_type in ("matches", "ntoMatches"):
            string = cond["string"]
            regex = cond["regex"]
            match = bool(re.search(regex, string))
            return match if cond_type == "matches" else not match

        if cond_type == "anyOf":
            conds = cond["conditions"]
            for cond in conds:
                if self.__evaluate_condition(cond):
                    return True
            return False

        if cond_type == "allOf":
            conds = cond["conditions"]
            for cond in conds:
                if not self.__evaluate_condition(cond):
                    return False
            return True

        if cond_type == "not":
            cond = cond["condition"]
            return not self.__evaluate_condition(cond)

        raise RuntimeError(f"Invalid cmake preset condition: {cond}")

    def __load_presets(self) -> None:
        if self.has_presets and not self.presets:
            self.presets = cmakepresets.CMakePresets(self.presets_path)
            self.__load_configure_presets()

    def __load_build_presets(self) -> None:
        if self.has_presets and self.preset:
            # Get all build presets related to the selected configure preset
            self.build_presets = self.presets.find_related_presets(self.preset["name"], "build")["build"]
            # Flatten build presets
            self.build_presets = [self.presets.resolve_macro_values("build", b["name"]) for b in self.build_presets]
            # Filter out hidden presets
            self.build_presets = [b for b in self.build_presets if not b.get("hidden", False)]
            # Filter out disabled presets
            self.build_presets = [b for b in self.build_presets if self.__evaluate_condition(b.get("condition", None))]

    def __load_configure_presets(self) -> None:
        # Flatten presets and resolve macros
        self.configure_presets = [self.presets.resolve_macro_values("configure", p["name"]) for p in self.presets.configure_presets]
        # Filter out hidden presets
        self.configure_presets = [p for p in self.configure_presets if not p.get("hidden", False)]
        # Filter out disabled presets
        self.configure_presets = [p for p in self.configure_presets if self.__evaluate_condition(p.get("condition", None))]
        # NOTE: We could hide presets with unsupported generator or when 
        #       the respective visual studio major version is missing,
        #       but that wouldn't match the default behaviour without presets


class CmakeConfigureCommand(ExecCommand):

    def __init__(self, window: sublime.Window) -> None:
        super().__init__(window)
        self.info = None  # type: Optional[CmakeInfo]
        self.__build_systems = []  # type: List[Dict[str, Any]]
        self.__error = None  # type: Optional[Exception]
        self.__response_handlers = {
            "codemodel": self.__handle_response_codemodel
        }  # type: Dict[str, Callable]

    def is_enabled(self) -> bool:
        try:
            self.info = CmakeInfo(self.window)
        except FileNotFoundError:
            return False
        return True

    def description(self) -> str:
        return 'Configure'

    def __on_load_done(self):
        # Called, when cmake info is done loading

        cmd = self.info.to_command()
        if get_setting(self.window.active_view(),
                       "silence_developer_warnings", False):
            cmd.append("-Wno-dev")
        write_query(self.window, self.info.build_folder)
        self.window.status_message("Generating build system...")

        super().run(
            cmd=cmd,
            working_dir=self.info.root_folder,
            file_regex=r'CMake\s(?:Error|Warning)(?:\s\(dev\))?\sat\s(.+):(\d+)()\s?\(?(\w*)\)?:',
            syntax=syntax("Configure"),
            env=self.info.env)

    def run(self) -> None:
        if self.info is None:
            assert self.is_enabled()
        assert self.info is not None
        if capabilities("fileApi") is None:
            sublime.error_message(
                " ".join((
                    "No support for the file API. ",
                    "This was introduced in cmake version 3.15. You have ",
                    "version {}. You can download a recent CMake version from ",
                    "www.cmake.org"
                )).format(
                    cast(
                        dict,
                        capabilities("version"))["string"]
                    )
            )
            return
        if get_setting(self.window.active_view(),
                       "always_clear_cache_before_configure", False):
            self.window.run_command("cmake_clear_cache",
                                    {"with_confirmation": False})

        self.info.load(self.__on_load_done)

    def on_finished(self, proc):
        log("finished running cmake")
        super().on_finished(proc)
        exit_code = proc.exit_code()
        if exit_code == 0 or exit_code is None:
            self.window.status_message("Translating...")
            self.__parse_file_api()
            self.__handle_build_presets()
            sublime.set_timeout(self.__write_project_data, 0)
        else:
            self.__erase_status()
            log("exited with an error")

    def __handle_build_presets(self):
        build_presets = self.info.get_build_presets()
        for b in build_presets:
            name = b["name"]
            friendly_name = b.get("displayName", name)
            build_system = {
                "name": f"Preset - {friendly_name}",
                "target": "cmake_build_preset",
                "working_dir": self.info.unexpanded_root_folder,
                "build_dir": self.info.unexpanded_build_folder,
                "preset": b["name"],
                "env": self.info.env,
                "generator": self.info.generator
            }
            self.__build_systems.append(build_system)

    def __parse_file_api(self):
        if self.info is None:
            raise RuntimeError("missing CMakeInfo data")
        log("parsing file api response")
        reply = load_reply(self.info.build_folder)
        responses = reply["reply"][CLIENT_STR]["query.json"]["responses"]
        for response in responses:
            try:
                self.__handle_response(response)
            except Exception as e:
                sublime.error_message("Error parsing response: {}".format(e))

    def __load_reply_json_file(self, json_file: str) -> dict:
        assert self.info
        path = join(file_api_reply(self.info.build_folder), json_file)
        with open(path, "r") as fp:
            return json.load(fp)

    def __handle_response(self, response: dict) -> None:
        data = self.__load_reply_json_file(response["jsonFile"])
        kind = response["kind"]
        handler = self.__response_handlers.get(kind)
        if not handler:
            log('no response handler installed for "{}"'.format(kind))
            return
        handler(data)

    def __handle_response_codemodel(self, data: dict) -> None:
        log("parsing codemodel")
        self.__error = None
        self.__build_systems = []
        assert self.info
        try:
            configurations = data["configurations"]
            for configuration in configurations:
                name = configuration["name"]
                if not name:
                    # Single-configuration generator and not CMAKE_BUILD_TYPE
                    # specified in the command line overrides
                    name = "Default"
                build_system = {
                    "name": name,
                    "config": name,
                    "target": "cmake_build",
                    "working_dir": self.info.unexpanded_root_folder,
                    "build_dir": self.info.unexpanded_build_folder,
                    "env": self.info.env}
                if self.info.generator:
                    build_system["generator"] = self.info.generator
                targets = configuration["targets"]
                variants = []  # type: List[Dict[str, Any]]
                for target in targets:
                    data = self.__load_reply_json_file(target["jsonFile"])
                    self.__handle_target(variants, name, data)
                directory = configuration.get("directories", [None])[0]
                if directory.get("hasInstallRule", False):
                    variants.append({"name": "install", "build_target": "install"})

                variants.append({"name": "ctest", "target": "ctest_run"})

                build_system["variants"] = variants
                self.__build_systems.append(build_system)
        except Exception as ex:
            self.__error = ex

    def __handle_target(self, variants: 'List[Dict[str, Any]]', config: str,
                        data: dict) -> None:
        name = data["name"]
        log("parsing target", name, "for config", config)
        variants.append({"name": name, "build_target": name})
        if data["type"] == "EXECUTABLE":
            artifacts = data["artifacts"]
            name_on_disk = data["nameOnDisk"]
            artifacts = [a["path"] for a in artifacts
                         if a["path"].endswith(name_on_disk)]
            if len(artifacts) == 0:
                log("no suitable artifact for target", name)
                return
            if len(artifacts) > 1:
                log("too many candidate artifacts for target", name)
                return
            variants.append({
                "name": "Run: " + name,
                "build_target": name,
                "target": "cmake_run",
                "artifact": artifacts[0]})
            if sublime.platform() == "linux":
                variants.append({
                    "name": "Run under GDB: " + name,
                    "build_target": name,
                    "target": "cmake_run",
                    "artifact": artifacts[0],
                    "debug": True})
            elif sublime.platform() == "osx":
                variants.append({
                    "name": "Run under LLDB: " + name,
                    "build_target": name,
                    "target": "cmake_run",
                    "artifact": artifacts[0],
                    "debug": True})

    def __write_project_data(self) -> None:
        if self.__error:
            sublime.error_message(
                "Error while configuring project: {}".format(
                    str(self.__error)))
            return
        log("writing project data")
        data = self.window.project_data()

        def is_not_generated_by_us(d):
            return "cmake_build" != d.get("target", "foo")

        bs = filter(is_not_generated_by_us, data.get("build_systems", []))
        data.update({"build_systems": list(bs) + self.__build_systems})
        self.window.set_project_data(data)
        self.window.status_message(
            "Generated build system! Select it in [Tools] -> [Build system]")


# Note: Things in "CMakeFiles" folders get removed anyway. This is where you put
# files that should be removed but are not inside CMakeFiles folders.
TRY_TO_REMOVE = [
    'CMakeCache.txt',
    'cmake_install.cmake'
]

class CmakeClearCacheCommand(sublime_plugin.WindowCommand):
    """Clears the CMake-generated files"""

    def is_enabled(self):
        try:
            self.info = CmakeInfo(self.window)
        except FileNotFoundError:
            return False
        return True

    @classmethod
    def description(cls):
        return 'Clear Cache'

    def run(self, with_confirmation=True):
        assert self.info
        build_folder = self.info.build_folder
        files_to_remove = []
        dirs_to_remove = []
        cmakefiles_dir = os.path.join(build_folder, 'CMakeFiles')
        if os.path.exists(cmakefiles_dir):
            for root, dirs, files in os.walk(cmakefiles_dir, topdown=False):
                files_to_remove.extend(
                    [os.path.join(root, name) for name in files])
                dirs_to_remove.extend(
                    [os.path.join(root, name) for name in dirs])
            dirs_to_remove.append(cmakefiles_dir)

        def append_file_to_remove(relative_name):
            abs_path = os.path.join(build_folder, relative_name)
            if os.path.exists(abs_path):
                files_to_remove.append(abs_path)

        for file in TRY_TO_REMOVE:
            append_file_to_remove(file)

        if not with_confirmation:
            self.remove(files_to_remove, dirs_to_remove)
            return

        panel = self.window.create_output_panel('files_to_be_deleted')

        self.window.run_command('show_panel',
            {'panel': 'output.files_to_be_deleted'})

        panel.run_command('insert',
            {'characters': 'Files to remove:\n' +
             '\n'.join(files_to_remove + dirs_to_remove)})

        def on_done(selected):
            if selected != 0: return
            self.remove(files_to_remove, dirs_to_remove)
            panel.run_command('append',
                {'characters': '\nCleared CMake cache files!',
                 'scroll_to_end': True})

        self.window.show_quick_panel(['Do it', 'Cancel'], on_done,
            sublime.KEEP_OPEN_ON_FOCUS_LOST)

    def remove(self, files_to_remove, dirs_to_remove):
        for file in files_to_remove:
            try:
                os.remove(file)
            except Exception:
                sublime.error_message('Cannot remove '+file)
        for directory in dirs_to_remove:
            try:
                os.rmdir(directory)
            except Exception:
                sublime.error_message('Cannot remove '+directory)


class CmakeOpenBuildFolderCommand(sublime_plugin.WindowCommand):
    """Opens the build folder."""

    def is_enabled(self) -> bool:
        try:
            self.info = CmakeInfo(self.window)
        except FileNotFoundError:
            return False
        return True

    @classmethod
    def description(cls):
        return "Browse Build Folder..."

    def run(self):
        if not self.info:
            if not self.is_enabled():
                return
        args = {"dir": realpath(self.info.build_folder)}
        self.window.run_command("open_dir", args=args)


class Diag:
    def __init__(self, check_name: str, ok_value: str, error_suggestion: str) -> None:
        self.__check_name: str = check_name
        self.__ok_value: str = ok_value
        self.__error_suggestion: str = error_suggestion

    def is_error(self) -> bool:
        return not bool(self.__ok_value)

    def ok_value(self) -> str:
        return self.__ok_value

    def error_suggestion(self) -> str:
        return self.__error_suggestion

    def minihtml(self) -> List[str]:
        result = ["<div class='check'><h2>Check: ", self.__check_name, "</h2>"]
        if self.is_error():
            result.extend([
                "<ul>",
                # "<li>", "<p>Current Value: ", str(item[1]), "</p>", "</li>",
                "<li>", "<p>Problem! Suggested Action: ", self.__error_suggestion, "</p>", "</li>",
                "</ul>"
            ])
        else:
            result.extend([
                "<ul>",
                "<li>", "<p>Current Value: ", self.__ok_value, "</p>", "</li>",
                "<li>", "<p>No problem here.</p>", "</li>",
                "</ul>"
            ])
        result.append("</div>")
        return result


class CmakeInsertDiagnosis:
    # TODO: When using presets, and generator is set both in preset and cmake 
    #       settings, issue a warning. If they are different, options from preset
    #       might not be compatible.

    def __init__(self, view: sublime.View) -> None:
        self.view = view

    def run(self, callback):
        self.__callback = callback
        self.__table: List[Diag] = []
        if   not self.__check_cmake_binary():   pass
        elif not self.__check_cmake_version():  pass
        elif not self.__check_cmake_settings(): pass

    def __check_cmake_binary(self) -> bool:
        self.__table.append(Diag("cmake binary", get_cmake_binary(), ""))
        return True

    def __append(self, info: str, val: Any, suggestion: str) -> None:
        self.__table.append(Diag(info, str(val), suggestion))

    def __ok(self, key: str, val: Any) -> None:
        self.__append(key, val, "")

    def __fail(self, key: str, suggestion: str) -> None:
        self.__append(key, False, suggestion)

    def __check_cmake_version(self) -> bool:
        try:
            output = check_output(
                "{} --version".format(get_cmake_binary())).splitlines()[0][14:]
        except Exception as e:
            self.__fail("cmake present", "Install cmake")
            return False
        else:
            self.__ok("cmake version", output)
        file_api = capabilities("fileApi")
        if file_api is not None:
            self.__ok("File API", True)
        else:
            self.__fail("File API", "Download cmake version >= 3.15")
            return False
        return True

    def __on_load_done(self):
        self.__ok("build_folder", self.info.build_folder)
        self.__ok("generator", self.info.generator)
        if self.info.platform:
            self.__ok("platform", self.info.platform)
        if self.info.toolset:
            self.__ok("toolset", self.info.toolset)
        if self.info.vs_major_version:
            self.__ok("selected vs major ver", self.info.vs_major_version)
        self.__ok("command to be run", self.info)
        self.__on_check_cmake_settings_complete(True)

    def __on_check_cmake_settings_complete(self, ok: bool):
        if not ok: pass
        self.__callback(tabulate(self.__table))

    def __check_cmake_settings(self) -> bool:
        try:
            self.window = self.view.window()
            if self.window:
                self.info = CmakeInfo(self.window)
                self.info.load(self.__on_load_done)
            else:
                raise RuntimeError("failed to load window")
        except FileNotFoundError:
            self.__fail("CMakeLists.txt present",
                        "Make sure you have a CMakeLists.txt")
            self.__on_check_cmake_settings_complete(False)

class CmakeDiagnoseCommand(sublime_plugin.WindowCommand):

    def __on_insert_diagnosis_complete(self, res):
        self.window.new_html_sheet("CMakeBuilder Diagnosis",
                                   res)

    def run(self):
        view = self.window.active_view()
        if not view:
            return
        CmakeInsertDiagnosis(view).run(self.__on_insert_diagnosis_complete)


    @classmethod
    def description(cls):
        return "Diagnose (Help! What should I do?)"

def tabulate(data: List[Diag]) -> str:
    result: List[str] = []
    result.append("<h1>Diagnosis</h1>")
    for item in data:
        result.extend(item.minihtml())
    return "".join(result)
