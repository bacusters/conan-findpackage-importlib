import os
import textwrap
from this import d

from jinja2 import Template, Environment, DictLoader, FileSystemLoader, select_autoescape

from conans.client.generators.cmake import DepsCppCmake
from conans.client.generators.cmake_find_package_common import (
    find_transitive_dependencies)
from conans.client.generators.cmake_multi import extend
from conans.util.files import save
from conans.client.tools.oss import OSInfo
from conans.client.tools.win import vcvars_dict, environment_append
from conans import ConanFile
from conans.model import Generator
from conans.model.conan_generator import GeneratorComponentsMixin

from contextlib import contextmanager
import subprocess
import glob
import copy
from pathlib import Path
from IndentedPrint import IndentedPrint
from ImportLibraryTypeDeduction import ImportLibraryTypeDeduction


class PackageSpec:
    def __init__(self, pkg_name: str, cpp_info):
        self.name = pkg_name
        self.filename = self._get_filename(cpp_info)
        self.findname = self._get_name(cpp_info)
        self.namespace = self.findname
        self.version = cpp_info.version
        self.public_deps_filenames = []
        self.deps_names = []


class BuildTypeSpec:
    def __init__(self):
        self.build_type = ''
        self.build_type_suffix = ''


@contextmanager
def run_with_env(env_dict):
    with environment_append(env_dict):
        yield


class CmakeFilters:

    @staticmethod
    def cmake_apply_filter(paths, filter_obj):
        if not 'filter' in filter_obj or filter_obj['filter'] == 'None':
            return paths
        if hasattr(CmakeFilters, filter_obj['filter']):
            if 'filterargs' in filter_obj:
                return getattr(CmakeFilters, filter_obj['filter']).__call__(paths, *filter_obj['filterargs'])
            else:
                return getattr(CmakeFilters, filter_obj['filter']).__call__(paths)
        else:
            raise Exception("Unknown filter {}".format(filter_obj['filter']))

    @staticmethod
    def cmake_pathsjoin(paths):
        """
        Paths are doubled quoted, and escaped (but spaces)
        e.g: set(LIBFOO_INCLUDE_DIRS "/path/to/included/dir" "/path/to/included/dir2")
        """
        return "\n\t\t\t".join('"%s"'
                               % p.replace('\\', '/').replace('$', '\\$').replace('"', '\\"')
                               for p in paths)

    @staticmethod
    def cmake_flagsjoin(values, separator=' '):
        # Flags have to be escaped
        return separator.join(v.replace('\\', '\\\\').replace('$', '\\$').replace('"', '\\"')
                              for v in values)

    @staticmethod
    def cmake_definesjoin(values, prefix=""):
        # Defines have to be escaped, included spaces
        return "\n\t\t\t".join('"%s%s"' % (prefix, v.replace('\\', '\\\\').replace('$', '\\$').
                                           replace('"', '\\"'))
                               for v in values)

    @staticmethod
    def cmake_pathsjoinsingle(values):
        """
        semicolon-separated list of dirs:
        e.g: set(LIBFOO_INCLUDE_DIR "/path/to/included/dir;/path/to/included/dir2")
        """
        return '"%s"' % ";".join(p.replace('\\', '/').replace('$', '\\$') for p in values)

    @staticmethod
    def format_link_flags(link_flags):
        # Trying to mess with - and / => https://github.com/conan-io/conan/issues/8811
        return link_flags


class CMakeData:

    cmake_version_check = textwrap.dedent("""
        # Requires CMake > 3.0
        if(${{CMAKE_VERSION}} VERSION_LESS "3.0")
            message(FATAL_ERROR "The 'cmake_find_package_multi' generator only works with CMake > 3.0")
        endif()
    """)
    conan_message = textwrap.dedent("""
        function(conan_message MESSAGE_OUTPUT)
            if(NOT CONAN_CMAKE_SILENT_OUTPUT)
                message(${ARGV${0}})
            endif()
        endfunction()
    """)
    apple_frameworks_macro = textwrap.dedent("""
        macro(conan_find_apple_frameworks FRAMEWORKS_FOUND FRAMEWORKS FRAMEWORKS_DIRS)
            if(APPLE)
                foreach(_FRAMEWORK ${FRAMEWORKS})
                    # https://cmake.org/pipermail/cmake-developers/2017-August/030199.html
                    find_library(CONAN_FRAMEWORK_${_FRAMEWORK}_FOUND NAME ${_FRAMEWORK} PATHS ${FRAMEWORKS_DIRS} CMAKE_FIND_ROOT_PATH_BOTH)
                    if(CONAN_FRAMEWORK_${_FRAMEWORK}_FOUND)
                        list(APPEND ${FRAMEWORKS_FOUND} ${CONAN_FRAMEWORK_${_FRAMEWORK}_FOUND})
                    else()
                        message(FATAL_ERROR "Framework library ${_FRAMEWORK} not found in paths: ${FRAMEWORKS_DIRS}")
                    endif()
                endforeach()
            endif()
        endmacro()
    """)
    conan_package_library_targets = textwrap.dedent("""
        # We assume version at least 3.0, preferably 3.19 or higher.
        function(conan_package_library_targets)
            # Old args: libraries package_libdir deps out_libraries out_libraries_target build_type package_name
            set(_FLAGS HAS_IMPORTLIB)
            set(_KV_ARGS LIB_TYPE OUT_LIBS OUT_LIB_TARGETS BUILD_TYPE PACKAGE_NAME IMPORTED_LOCATION)
            set(_K_MULTI_V_ARGS LIBRARIES LIBDIRS DEPENDENDCIES)
            cmake_parse_arguments(IN "${_FLAGS}" "${_KV_ARGS}" "${_K_MULTI_V_ARGS}" ${ARGN})
            
            unset(_CONAN_ACTUAL_TARGETS CACHE)
            unset(_CONAN_FOUND_SYSTEM_LIBS CACHE)
            foreach(_LIBRARY_NAME ${IN_LIBRARIES})
                find_library(CONAN_FOUND_LIBRARY NAME ${_LIBRARY_NAME} PATHS ${IN_LIBDIRS}
                             NO_DEFAULT_PATH NO_CMAKE_FIND_ROOT_PATH)
                if(CONAN_FOUND_LIBRARY)
                    conan_message(STATUS "Library ${_LIBRARY_NAME} found ${CONAN_FOUND_LIBRARY}")
                    list(APPEND _out_libraries ${CONAN_FOUND_LIBRARY})
                
                    # Create a micro-target for each lib/a found
                    string(REGEX REPLACE "[^A-Za-z0-9.+_-]" "_" _LIBRARY_NAME ${_LIBRARY_NAME})
                    set(_LIB_NAME CONAN_LIB::${IN_PACKAGE_NAME}_${_LIBRARY_NAME}${IN_BUILD_TYPE})
                    if(NOT TARGET ${_LIB_NAME})
                        # Create a micro-target for each lib/a found
                        if(${IN_LIB_TYPE})
                            add_library(${_LIB_NAME} ${IN_LIB_TYPE} IMPORTED)
                        else()
                            add_library(${_LIB_NAME} UNKNOWN IMPORTED)
                        endif()
                        if(IN_HAS_IMPORTLIB)
                            set_target_properties(${_LIB_NAME} PROPERTIES IMPORTED_LOCATION ${IN_IMPORTED_LOCATION})
                            set_target_properties(${_LIB_NAME} PROPERTIES IMPORTED_IMPLIB ${CONAN_FOUND_LIBRARY})
                        else()
                            set_target_properties(${_LIB_NAME} PROPERTIES IMPORTED_LOCATION ${CONAN_FOUND_LIBRARY})
                        endif()
                        set(_CONAN_ACTUAL_TARGETS ${_CONAN_ACTUAL_TARGETS} ${_LIB_NAME})
                    else()
                        conan_message(STATUS "Skipping already existing target: ${_LIB_NAME}")
                    endif()
                    list(APPEND _out_libraries_target ${_LIB_NAME})
                    
                    conan_message(STATUS "Found: ${CONAN_FOUND_LIBRARY}")
                else()
                    conan_message(STATUS "Library ${_LIBRARY_NAME} not found in package, might be system one")
                    list(APPEND _out_libraries_target ${_LIBRARY_NAME})
                    list(APPEND _out_libraries ${_LIBRARY_NAME})
                    set(_CONAN_FOUND_SYSTEM_LIBS "${_CONAN_FOUND_SYSTEM_LIBS};${_LIBRARY_NAME}")
                endif()
                unset(CONAN_FOUND_LIBRARY CACHE)
            endforeach()

            # Add all dependencies to all targets
            string(REPLACE " " ";" deps_list "${IN_DEPENDENDCIES}")
            foreach(_CONAN_ACTUAL_TARGET ${_CONAN_ACTUAL_TARGETS})
                set_property(TARGET ${_CONAN_ACTUAL_TARGET} PROPERTY INTERFACE_LINK_LIBRARIES "${_CONAN_FOUND_SYSTEM_LIBS};${deps_list}")
            endforeach()
            
            set(${IN_OUT_LIBS} ${_out_libraries} PARENT_SCOPE)
            set(${IN_OUT_LIB_TARGETS} ${_out_libraries_target} PARENT_SCOPE)
        endfunction()
    """)


class CMakeFindPackageMultiGeneratorCustom(GeneratorComponentsMixin, Generator):
    name = "cmake_find_package_multi_custom"
    # Mapping from CMake variable type to key in cpp_info object of Conan.
    component_vars = {
        'INCLUDE_DIRS': dict(key='include_paths', filter='cmake_pathsjoin'),
        'INCLUDE_DIR': dict(key='include_paths', filter='cmake_pathsjoinsingle'),
        'INCLUDES': dict(key='include_paths', filter='cmake_pathsjoin'),
        'LIB_DIRS': dict(key='lib_paths', filter='cmake_pathsjoin'),
        'RES_DIRS': dict(key='res_paths', filter='cmake_pathsjoin'),
        'DEFINITIONS': dict(key='defines', filter='cmake_definesjoin', filterargs=['-D']),
        'COMPILE_DEFINITIONS': dict(key='defines', filter='cmake_definesjoin'),
        'COMPILE_OPTIONS_C': dict(key='cflags', quoted=True, filter='cmake_flagsjoin', filterargs=[';']),
        'COMPILE_OPTIONS_CXX': dict(key='cxxflags', quoted=True, filter='cmake_flagsjoin', filterargs=[';']),
        'LIBS': dict(key='libs', filter='cmake_flagsjoin', filterargs=[' ']),
        'SYSTEM_LIBS': dict(key='system_libs', filter='cmake_flagsjoin', filterargs=[' ']),
        'FRAMEWORK_DIRS': dict(key='framework_paths', filter='cmake_pathsjoin'),
        'FRAMEWORKS': dict(key='frameworks', filter='cmake_flagsjoin', filterargs=[' ']),
        'BUILD_MODULES_PATHS': dict(key='build_modules_paths'),
        'DEPENDENCIES': dict(key='public_deps')
    }

    def __init__(self, conanfile):
        super(CMakeFindPackageMultiGeneratorCustom, self).__init__(conanfile)
        self.configuration = str(self.conanfile.settings.build_type)
        self.configurations = [
            v for v in conanfile.settings.build_type.values_range if v != "None"]
        # FIXME: Ugly way to define the output path
        self.output_path = os.getcwd()

        self.template_env = Environment(
            loader=FileSystemLoader(str(Path(__file__).parent.resolve())),
            autoescape=select_autoescape()
        )
        self._macros_and_functions = "\n".join([
            CMakeData.conan_message,
            CMakeData.apple_frameworks_macro,
            CMakeData.conan_package_library_targets,
        ])
        CMakeFindPackageMultiGeneratorCustom.setup_cmake_filters(
            self.template_env)

        self.library_deduce = ImportLibraryTypeDeduction(conanfile)

    @staticmethod
    def setup_cmake_filters(env: Environment):
        env.filters['cmake_val'] = lambda x: '${'+x+'}'
        env.filters['cmake_value'] = lambda x: '${'+x+'}'
        env.filters['cmake_pathsjoin'] = CmakeFilters.cmake_pathsjoin
        env.filters['cmake_flagsjoin'] = CmakeFilters.cmake_flagsjoin
        env.filters['cmake_definesjoin'] = CmakeFilters.cmake_definesjoin
        env.filters['cmake_pathsjoinsingle'] = CmakeFilters.cmake_pathsjoinsingle
        env.filters['cmake_apply_filter'] = CmakeFilters.cmake_apply_filter
        env.filters['list_format'] = lambda values, formatstr: [
            formatstr.format(v) for v in values]

    def generate(self):
        generator_files = self.content
        for generator_file, content in generator_files.items():
            generator_file = os.path.join(self.output_path, generator_file)
            save(generator_file, content)

    @property
    def filename(self):
        return None

    @classmethod
    def _get_filename(cls, obj):
        return obj.get_filename(cls.name)

    def _get_components_of_dependency(self, pkg_name, cpp_info):
        components = super(CMakeFindPackageMultiGeneratorCustom,
                           self)._get_components(pkg_name, cpp_info)
        ret = []
        for comp_genname, comp, comp_requires_gennames in components:
            deps_cpp_cmake = copy.deepcopy(comp)

            deps_cpp_cmake.public_deps = " ".join(
                ["{}::{}".format(*it) for it in comp_requires_gennames])
            deps_cpp_cmake.build_module_paths = cpp_info.build_modules_paths.get(
                self.name, [])
            import_lib_info = self.import_library_info_from_cppinfo(
                deps_cpp_cmake)
            deps_cpp_cmake.import_lib_info = import_lib_info
            ret.append((comp_genname, deps_cpp_cmake))
        return ret

    def _render_template_str(self, template_name, **kwargs):
        # Add some defaults that we always expose
        return self.template_env.get_template(template_name).render(
            cmake_version_check=CMakeData.cmake_version_check,
            macros_and_functions=self._macros_and_functions,
            **kwargs)

    def _render_template(self, template_name, output_name, output: dict[str, str], **kwargs):
        output[output_name] = self._render_template_str(
            template_name, **kwargs)

    def generate_dependency_with_components(self, cpp_info, pkg: PackageSpec, bt: BuildTypeSpec, output_files: dict[str, str]):
        cpp_info = extend(cpp_info, bt.build_type.lower())

        cpp_info.import_lib_info = self.library_deduce.import_library_info_from_cppinfo(
            cpp_info)
        # Tuple of name, weird FindPackageGen object and cpp_info
        components = self._get_components_of_dependency(pkg.name, cpp_info)

        # Note these are in reversed order, from more dependent to less dependent
        pkg_components = " ".join(["{p}::{c}".format(p=pkg.namespace, c=comp_findname) for
                                   comp_findname, _ in reversed(components)])
        render_args = dict(pkg=pkg,
                           components=components,
                           build_type=bt.build_type
                           )
        self._render_template('target_buildtype_components.jinja', self._targets_filename(pkg.filename, bt.build_type.lower()), output_files,
                              **render_args,
                              component_vars=self.component_vars,
                              pkg_components=pkg_components,
                              deps=cpp_info
                              )
        self._render_template('targets.jinja', self._targets_filename(pkg.filename), output_files,
                              **render_args
                              )
        self._render_template('config_components.jinja', self._config_filename(pkg.filename), output_files,
                              **render_args,
                              pkg_public_deps=pkg.public_deps_filenames,
                              configs=self.configurations
                              )

    def generate_dependency_without_components(self, cpp_info, pkg: PackageSpec, bt: BuildTypeSpec, output_files: dict[str, str]):
        self._render_template('config.jinja', self._config_filename(pkg.filename), output_files,
                              filename=pkg.filename,
                              name=pkg.findname,
                              namespace=pkg.namespace,
                              version=cpp_info.version,
                              public_deps_filenames=pkg.public_deps_filenames
                              )

        # If any config matches the build_type one, add it to the cpp_info
        dep_cpp_info = extend(cpp_info, bt.build_type.lower())

        # Get import type
        dep_cpp_info.import_lib_info = self.library_deduce.import_library_info_from_cppinfo(
            dep_cpp_info)

        # Targets of the package
        self._render_template('targets.jinja', self._targets_filename(pkg.filename), output_files,
                              pkg=pkg,
                              deps=dep_cpp_info,
                              import_type=dep_cpp_info.import_lib_info
                              )

        #deps = DepsCppCmake(dep_cpp_info, self.name)
        # Config for build type
        self._render_template('target_buildtype_single.jinja', self._targets_filename(pkg.filename, self.configuration.lower()), output_files,
                              component_vars=self.component_vars,
                              name=pkg.findname, deps=dep_cpp_info,
                              pkg=pkg,
                              build_type=bt.build_type,
                              deps_names=pkg.deps_names)

    def generate_dependency_files(self, output_files: dict[str, str], pkg_name: str, cpp_info, buildtype_spec: BuildTypeSpec):
        self._validate_components(cpp_info)
        pkg = PackageSpec(pkg_name, cpp_info)

        public_deps = self.get_public_deps(cpp_info)
        deps_names = []
        for it in public_deps:
            name = "{}::{}".format(*self._get_require_name(*it))
            if name not in deps_names:
                deps_names.append(name)
        pkg.deps_names = ';'.join(deps_names)
        pkg.public_deps_filenames = [self._get_filename(self.deps_build_info[it[0]]) for it in
                                     public_deps]
        # Generate version file
        self._render_template('config_version.jinja', self._config_version_filename(pkg.filename), output_files,
                              version=pkg.version
                              )
        # Generate dependency files
        if not cpp_info.components:
            self.generate_dependency_without_components(
                cpp_info, pkg, buildtype_spec, output_files)
        else:
            self.generate_dependency_with_components(
                cpp_info, pkg, buildtype_spec, output_files)

    @property
    def content(self):
        ret = {}
        buildtype_spec = BuildTypeSpec()
        buildtype_spec.build_type = str(
            self.conanfile.settings.build_type).upper()
        buildtype_spec.build_type_suffix = "_{}".format(
            self.configuration.upper()) if self.configuration else ""

        for pkg_name, cpp_info in self.deps_build_info.dependencies:
            self.generate_dependency_files(
                ret, pkg_name, cpp_info, buildtype_spec)
        return ret

    def _targets_filename(self, pkg_filename, build_type=None):
        if build_type is None:
            return "{}Target.cmake".format(pkg_filename)
        return "{}Target-{}.cmake".format(pkg_filename, build_type)

    def _config_filename(self, pkg_filename):
        if pkg_filename == pkg_filename.lower():
            return "{}-config.cmake".format(pkg_filename)
        else:
            return "{}Config.cmake".format(pkg_filename)

    def _config_version_filename(self, pkg_filename):
        if pkg_filename == pkg_filename.lower():
            return "{}-config-version.cmake".format(pkg_filename)
        else:
            return "{}ConfigVersion.cmake".format(pkg_filename)

    def _config(self, filename, name, namespace, version, public_deps_names):
        # Builds the XXXConfig.cmake file for one package

        # The common macros
        macros_and_functions = "\n".join([
            CMakeData.conan_message,
            CMakeData.apple_frameworks_macro,
            CMakeData.conan_package_library_targets,
        ])

        # Fix public dependencies to be an array
        if not public_deps_names:
            public_deps_names = []

        tmp = self._render_template_str('config.jinja',
                                        name=name, version=version,
                                        namespace=namespace,
                                        configs=self.configurations,
                                        filename=filename,
                                        cmake_version_check=CMakeData.cmake_version_check,
                                        macros_and_functions=macros_and_functions,
                                        public_deps_filenames=public_deps_names)
        return tmp


class CustomConanCmakeGen(ConanFile):
    name = "customcmakegen"
    version = "0.1"
    url = "https://github.com/bacusters/customcmakegen"
    license = "MIT"
    exports = ['config_base.jinja',
'config_components.jinja',
'config_single.jinja',
'config_version.jinja',
'ImportLibraryTypeDeduction.py',
'IndentedPrint.py',
'README.md',
'target_buildtype_base.jinja',
'target_buildtype_components.jinja',
'target_buildtype_single.jinja',
'target_properties.jinja',
'targets.jinja']

