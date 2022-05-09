import os
import textwrap
from this import d

from jinja2 import Template, Environment, DictLoader, FileSystemLoader, select_autoescape

from conans.client.generators.cmake import DepsCppCmake
from conans.client.generators.cmake_find_package_common import (find_transitive_dependencies)
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


class PackageSpec:
    def __init__(self):
        self.name = ''
        self.filename = ''
        self.findname = ''
        self.namespace = ''
        self.version = ''
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
    def cmake_flagsjoin(values,separator=' '):
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

    base_config_tpl = textwrap.dedent("""
        ########## MACROS ###########################################################################
        #############################################################################################
        {{macros_and_functions}}

        {{cmake_version_check}}

        include(${CMAKE_CURRENT_LIST_DIR}/{{filename}}Targets.cmake)
        
        {% block find_dependencies %}
        ########## FIND DEPENDENDENCIES #############################################################
        #############################################################################################
        # Library dependencies
        include(CMakeFindDependencyMacro)
        {% for dep_filename in public_deps_filenames -%}
        if(NOT {{dep_filename}}_FOUND)
            if(${CMAKE_VERSION} VERSION_LESS "3.9.0")
                find_package({{dep_filename}} REQUIRED NO_MODULE)
            else()
                find_dependency({{dep_filename}} REQUIRED NO_MODULE)
            endif()
        else()
            message(STATUS "Dependency {{dep_filename}} already found")
        endif()
        {%- endfor %}
        {% endblock %}

        {% block target_props %}
        ########## TARGET PROPERTIES ################################################################
        #############################################################################################
        {% endblock %}
        ########## MODULES BUILD ####################################################################
        #############################################################################################
        {% block build_modules %}
        {% endblock %}
        """)

    config_tpl = textwrap.dedent("""
        {% extends "base_config.tpl" %}

        {% block target_props %}
        {% include 'target_properties.tpl' %}
        {% endblock %}

        {% block build_modules %}
        {% import 'build_modules.tpl' as mods %}
        {{ mods.include_build_modules(name,configs) }}
        {% endblock %}

        {% block find_dependencies %}
        # Library dependencies
        include(CMakeFindDependencyMacro)
        {% for dep_filename in public_deps_filenames -%}
        if(NOT {{dep_filename}}_FOUND)
            if(${CMAKE_VERSION} VERSION_LESS "3.9.0")
                find_package({{dep_filename}} REQUIRED NO_MODULE)
            else()
                find_dependency({{dep_filename}} REQUIRED NO_MODULE)
            endif()
        else()
            message(STATUS "Dependency {{dep_filename}} already found")
        endif()
        {%- endfor %}
        {% endblock %}
        """)

    comp_config_tpl = textwrap.dedent("""\
        {% extends "base_config.tpl" %}
        
        {% block target_props %}
        ########## TARGETS PROPERTIES ###############################################################
        #############################################################################################
        {%- for comp_name, comp in components %}
        {% set comp_target = pkg.namespace + "::" + comp_name %}
        {%- macro tvalue(var, config) -%}
        {{'${'+pkg.name+'_'+comp_name+'_'+var+'_'+config.upper()+'}'}}
        {%- endmacro -%}
        ########## COMPONENT {{ comp_name }} TARGET PROPERTIES ######################################

        set_target_properties({{comp_target}} 
        PROPERTIES
            INTERFACE_LINK_LIBRARIES
            {%- for config in configs %}
            $<$<CONFIG:{{config}}>:{{tvalue('LINK_LIBS', config)}} {{tvalue('LINKER_FLAGS_LIST', config)}}>
            {%- endfor %}
            
            INTERFACE_INCLUDE_DIRECTORIES
            {%- for config in configs %}
            $<$<CONFIG:{{config}}>:{{tvalue('INCLUDE_DIRS', config)}}>
            {%- endfor %}
            
            INTERFACE_COMPILE_DEFINITIONS
            {%- for config in configs %}
            $<$<CONFIG:{{config}}>:{{tvalue('COMPILE_DEFINITIONS', config)}}>
            {%- endfor %}
        
            INTERFACE_COMPILE_OPTIONS
            {%- for config in configs %}
            $<$<CONFIG:{{config}}>:
                {{tvalue('COMPILE_OPTIONS_C', config)}}
                {{tvalue('COMPILE_OPTIONS_CXX', config)}}>
            {%- endfor %})
        set({{ pkg.name }}_{{ comp_name }}_TARGET_PROPERTIES TRUE)

        {%- endfor %}

        ########## GLOBAL TARGET PROPERTIES #########################################################

        if(NOT {{ pkg.name }}_{{ pkg.name }}_TARGET_PROPERTIES)
            set_property(TARGET {{ pkg.namespace }}::{{ pkg.name }} APPEND PROPERTY INTERFACE_LINK_LIBRARIES
                         {%- for config in configs %}
                         $<$<CONFIG:{{config}}>:{{ pkg.name+'_COMPONENTS_'+config.upper()|cmake_val }}>
                         {%- endfor %})
        endif()
        {% endblock %}

        {% block build_modules %}
        ########## BUILD MODULES ####################################################################
        #############################################################################################
        {% import 'build_modules.tpl' as mods %}
        
        {%- for comp_name, comp in components %}

        ########## COMPONENT {{ comp_name }} BUILD MODULES ##########################################
        {{ mods.include_build_modules(pkg.name+'_'+comp_name,configs) }}
        {%- endfor %}
        {% endblock %}
        """)

    targets_tpl = textwrap.dedent("""\
        {%- for comp_name, comp in components %}
        {% set comp_target = pkg.namespace + '::' + comp_name %}
        if(NOT TARGET {{ comp_target }})
            add_library({{ comp_target }} {{comp.import_lib_info.import_type}} IMPORTED)
        endif()

        {%- endfor %}
        if(NOT TARGET {{ pkg.namespace }}::{{ pkg.name }})
            add_library({{ pkg.namespace }}::{{ pkg.name }} INTERFACE IMPORTED)
        endif()

        # Load the debug and release library finders
        set(_TARGET_PREFIX {{ pkg.filename }}Target)
        get_filename_component(_DIR "${CMAKE_CURRENT_LIST_FILE}" PATH)
        file(GLOB CONFIG_FILES "${_DIR}/${_TARGET_PREFIX}-*.cmake")

        foreach(f ${CONFIG_FILES})
            include(${f})
        endforeach()

        {% if components|length %} {# Non-empty components#}
        if({{ pkg_filename }}_FIND_COMPONENTS)
            foreach(_FIND_COMPONENT {{ pkg.filename+'_FIND_COMPONENTS' |cmake_val }})
                list(FIND {{ pkg.name }}_COMPONENTS_{{ build_type }} "{{ pkg.namespace }}::${_FIND_COMPONENT}" _index)
                if(${_index} EQUAL -1)
                    conan_message(FATAL_ERROR "Conan: Component '${_FIND_COMPONENT}' NOT found in package '{{ pkg.name }}'")
                else()
                    conan_message(STATUS "Conan: Component '${_FIND_COMPONENT}' found in package '{{ pkg.name }}'")
                endif()
            endforeach()
        endif()
        {% endif %}
        """)


    # This template takes the "name" of the target name::name and configs = ["Release", "Debug"..]
    target_properties_tpl = """
# Assign target properties
set_target_properties({{namespace}}::{{name}} PROPERTIES
INTERFACE_LINK_LIBRARIES
    {%- for config in configs %}
    $<$<CONFIG:{{config}}>:${{'{'}}{{name}}_LIBRARIES_TARGETS_{{config.upper()}}}
                        ${{'{'}}{{name}}_LINKER_FLAGS_{{config.upper()}}_LIST}>
    {%- endfor %}
INTERFACE_INCLUDE_DIRECTORIES
    {%- for config in configs %}
    $<$<CONFIG:{{config}}>:${{'{'}}{{name}}_INCLUDE_DIRS_{{config.upper()}}}>
    {%- endfor %}
INTERFACE_COMPILE_DEFINITIONS
    {%- for config in configs %}
    $<$<CONFIG:{{config}}>:${{'{'}}{{name}}_COMPILE_DEFINITIONS_{{config.upper()}}}>
    {%- endfor %}
INTERFACE_COMPILE_OPTIONS
    {%- for config in configs %}
    $<$<CONFIG:{{config}}>:${{'{'}}{{name}}_COMPILE_OPTIONS_{{config.upper()}}_LIST}>
    {%- endfor %}
)"""

    build_modules = textwrap.dedent("""
        {%- macro include_build_modules(prefix, configs) -%}
        # Build modules
        foreach(_BUILD_MODULE_PATH in LISTS {%- for config in configs -%}{{ prefix+'_BUILD_MODULES_PATHS_'+config.upper() | cmake_val }}{%- endfor -%})
            include(${_BUILD_MODULE_PATH})
        endforeach()
        {%- endmacro -%}
    """)

    # https://gitlab.kitware.com/cmake/cmake/blob/master/Modules/BasicConfigVersion-SameMajorVersion.cmake.in
    config_version_tpl = textwrap.dedent("""
        set(PACKAGE_VERSION "{version}")

        if(PACKAGE_VERSION VERSION_LESS PACKAGE_FIND_VERSION)
            set(PACKAGE_VERSION_COMPATIBLE FALSE)
        else()
            if("{version}" MATCHES "^([0-9]+)\\\\.")
                set(CVF_VERSION_MAJOR "${{CMAKE_MATCH_1}}")
            else()
                set(CVF_VERSION_MAJOR "{version}")
            endif()

            if(PACKAGE_FIND_VERSION_MAJOR STREQUAL CVF_VERSION_MAJOR)
                set(PACKAGE_VERSION_COMPATIBLE TRUE)
            else()
                set(PACKAGE_VERSION_COMPATIBLE FALSE)
            endif()

            if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
                set(PACKAGE_VERSION_EXACT TRUE)
            endif()
        endif()
        """)

    component_vars = { 
        'INCLUDE_DIRS' : dict(key='include_paths',filter='cmake_pathsjoin'),
        'INCLUDE_DIR':dict(key='include_paths', filter='cmake_pathsjoinsingle'),
        'INCLUDES':dict(key='include_paths',filter='cmake_pathsjoin'),
        'LIB_DIRS':dict(key='lib_paths',filter='cmake_pathsjoin'),
        'RES_DIRS':dict(key='res_paths',filter='cmake_pathsjoin'),
        'DEFINITIONS':dict(key='defines',filter='cmake_definesjoin',filterargs=['-D']),
        'COMPILE_DEFINITIONS':dict(key='defines',filter='cmake_definesjoin'),
        'COMPILE_OPTIONS_C':dict(key='cflags',quoted=True,filter='cmake_flagsjoin',filterargs=[';']),
        'COMPILE_OPTIONS_CXX':dict(key='cxxflags',quoted=True, filter='cmake_flagsjoin',filterargs=[';']),
        'LIBS':dict(key='libs',filter='cmake_flagsjoin',filterargs=[' ']),
        'SYSTEM_LIBS':dict(key='system_libs',filter='cmake_flagsjoin',filterargs=[' ']),
        'FRAMEWORK_DIRS':dict(key='framework_paths',filter='cmake_pathsjoin'),
        'FRAMEWORKS':dict(key='frameworks',filter='cmake_flagsjoin',filterargs=[' ']),
        'BUILD_MODULES_PATHS':dict(key='build_modules_paths'),
        'DEPENDENCIES':dict(key='public_deps')
    }

    base_target_buildtype_tpl = textwrap.dedent("""
    ########## MACROS ###########################################################################
    #############################################################################################
    {{ conan_message }}
    {{ conan_find_apple_frameworks }}
    {{ conan_package_library_targets }}

    {% block global_vars %}
    {% include 'global_target_buildtype.tpl' %}
    {% endblock %}

    {% block components %}
    {% endblock %}
    """)

    comp_target_buildtype_tpl = textwrap.dedent("""\
        {% extends 'base_target_buildtype.tpl' %}
        {% block global_vars %}
        {{ super() }}
        set({{ pkg.name }}_COMPONENTS_{{ build_type }} {{ pkg_components }})
        {% endblock %}

        {% block components %}
        {%- for comp_name, comp in components %}
        {# Helpers macros #}
        {%- macro tvalue(var) -%}
            {{ (pkg.name+'_'+comp_name+'_'+var+'_'+build_type.upper()) | cmake_val}}
        {%- endmacro -%}
        {%- macro tvar(var) -%}
            {{pkg.name+'_'+comp_name+'_'+var+'_'+build_type.upper()}}
        {%- endmacro -%}

        ########### COMPONENT {{ comp_name }} VARIABLES #############################################
        {%- for cmake_name, mapping in component_vars.items() -%}
        set({{ tvar(cmake_name)}} {{ comp[mapping['key']] | cmake_apply_filter(mapping)}})
        {%- endfor %}
        set({{ tvar('LINKER_FLAGS_LIST') }}
                $<$<STREQUAL:$<TARGET_PROPERTY:TYPE>,SHARED_LIBRARY>:{{ comp.sharedlinkflags_list }}>
                $<$<STREQUAL:$<TARGET_PROPERTY:TYPE>,MODULE_LIBRARY>:{{ comp.sharedlinkflags_list }}>
                $<$<STREQUAL:$<TARGET_PROPERTY:TYPE>,EXECUTABLE>:{{ comp.exelinkflags_list }}>
        )

        ########## COMPONENT {{ comp_name }} FIND LIBRARIES & FRAMEWORKS / DYNAMIC VARS #############
        set({{ tvar('FRAMEWORKS_FOUND')}} "")
        conan_find_apple_frameworks({{ tvar('FRAMEWORKS_FOUND')}} 
            "{{ tvalue('FRAMEWORKS')}}" "{{ tvalue('FRAMEWORK_DIRS') }}")

        set({{ tvar('LIB_TARGETS')}} "")
        set({{ tvar('NOT_USED')}} "")
        set({{ tvar('LIBS_FRAMEWORKS_DEPS')}} {{ tvalue('FRAMEWORKS_FOUND')}} 
            {{ tvalue('SYSTEM_LIBS') }} {{ tvalue('DEPENDENCIES') }})
        conan_package_library_targets(  LIBRARIES       "{{tvalue('LIBS')}}"
                                        LIB_TYPE        {{comp.import_lib_info.import_type}}
                                        LIBDIRS         "{{tvalue('LIB_DIRS')}}"
                                        DEPENDENCIES    "{{tvalue('LIBS_FRAMEWORKS_DEPS')}}"
                                        OUT_LIBS        {{tvar('NOT_USED')}}
                                        OUT_LIB_TARGETS {{tvar('LIB_TARGETS')}}
                                        BUILD_TYPE      "{{build_type}}"
                                        PACKAGE_NAME    "{{ pkg.name }}_{{ comp_name }}"
                                        HAS_IMPORTLIB   {{'ON' if comp.import_lib_info.has_importlib else 'OFF' }}
                                        IMPORTED_LOCATION {{comp.import_lib_info.dll_location|default('')}})

        set({{tvar('LINK_LIBS')}} {{ tvalue('LIB_TARGETS') }} {{ tvalue('LIBS_FRAMEWORKS_DEPS')}})

        {%- endfor %}
        {% endblock %}
        """)

    global_target_buildtype_tpl = textwrap.dedent("""
        {%- macro tvar(var) -%}
        {{pkg.name+'_'+var+'_'+build_type.upper() }}
        {%- endmacro -%}
        {%- macro tvalue(var) -%}
        {{(pkg.name+'_'+var+'_'+build_type.upper()) | cmake_value}}
        {%- endmacro -%}

        # Directly from Conan
        {%-for cmake_var, mapping in component_vars.items()%}
        set({{tvar(cmake_var)}} {{deps[mapping['key']] | cmake_apply_filter(mapping)}} )
        {%-endfor %}
        
        # Computed
        set({{tvar('LINKER_FLAGS')}}_LIST
                "$<$<STREQUAL:$<TARGET_PROPERTY:TYPE>,SHARED_LIBRARY>:{{deps.sharedlinkflags| cmake_flagsjoin}}>"
                "$<$<STREQUAL:$<TARGET_PROPERTY:TYPE>,MODULE_LIBRARY>:{{deps.sharedlinkflags| cmake_flagsjoin}}>"
                "$<$<STREQUAL:$<TARGET_PROPERTY:TYPE>,EXECUTABLE>:{{deps.exelinkflags | cmake_flagsjoin }}>"
        )
        set({{tvar('COMPILE_OPTIONS')}}_LIST "{{deps.cxxflags|cmake_flagsjoin(';')}}" "{{deps.cxxflags|cmake_flagsjoin(';')}}")
        set({{tvar('LIBRARIES_TARGETS')}} "") # Will be filled later, if CMake 3
        set({{tvar('LIBRARIES')}} "") # Will be filled later
        set({{tvar('LIBRARIES')}} "") # Same as {name}_LIBRARIES
        set({{tvar('FRAMEWORKS_FOUND')}} "") # Will be filled later
        
        conan_find_apple_frameworks({{tvar('FRAMEWORKS_FOUND')}} "{{tvalue('FRAMEWORKS')}}" 
            "{{tvalue('FRAMEWORK_DIRS')}}")

        mark_as_advanced(
            {{tvar('INCLUDE_DIRS')}}
            {{tvar('INCLUDE_DIR')}}
            {{tvar('INCLUDES')}}
            {{tvar('DEFINITIONS')}}
            {{tvar('LINKER_FLAGS')}}
            {{tvar('COMPILE_DEFINITIONS')}}
            {{tvar('COMPILE_OPTIONS')}}
            {{tvar('LIBRARIES')}}
            {{tvar('LIBS')}}
            {{tvar('LIBRARIES_TARGETS')}})

        # Find the real .lib/.a and add them to {{name}}_LIBS and {{name}}_LIBRARY_LIST
        set({{tvar('LIBRARY_LIST')}} {{deps.libs}})

        # Gather all the libraries that should be linked to the targets (do not touch existing variables):
        set(_{{tvar('DEPENDENCIES')}} "{{tvalue('FRAMEWORKS_FOUND')}} {{tvalue('SYSTEM_LIBS')}} {{deps_names}}")

        conan_package_library_targets(  LIBRARIES       "{{tvalue('LIBRARY_LIST')}}"
                                        LIBDIRS         "{{tvalue('LIB_DIRS')}}"
                                        DEPENDENCIES    "{{tvalue('DEPENDENCIES')}}"
                                        OUT_LIBS        {{tvar('LIBRARIES')}}
                                        OUT_LIB_TARGETS {{tvar('LIBRARIES_TARGETS')}}
                                        BUILD_TYPE      "{{build_type}}"
                                        PACKAGE_NAME    "{{name}}")

        set({{tvar('LIBS')}} {{tvalue('LIBRARIES')}})

        foreach(_FRAMEWORK {{tvalue('FRAMEWORKS_FOUND')}})
            list(APPEND {{tvar('LIBRARIES_TARGETS')}} ${_FRAMEWORK})
            list(APPEND {{tvar('LIBRARIES')}} ${_FRAMEWORK})
        endforeach()

        foreach(_SYSTEM_LIB {{tvalue('SYSTEM_LIBS')}})
            list(APPEND {{tvar('LIBRARIES_TARGETS')}} ${_SYSTEM_LIB})
            list(APPEND {{tvar('LIBRARIES')}} ${_SYSTEM_LIB})
        endforeach()

        # We need to add our requirements too
        set({{tvar('LIBRARIES_TARGETS')}} "{{tvalue('LIBRARIES_TARGETS')}};{{deps_names}}")
        set({{tvar('LIBRARIES')}} "{{tvalue('LIBRARIES')}};{{deps_names}}")

        set(CMAKE_MODULE_PATH {{deps.build_paths|cmake_pathsjoin}} ${CMAKE_MODULE_PATH})
        set(CMAKE_PREFIX_PATH {{deps.build_paths|cmake_pathsjoin}} ${CMAKE_PREFIX_PATH})
        """)

    def __init__(self, conanfile):
        super(CMakeFindPackageMultiGeneratorCustom, self).__init__(conanfile)
        self.configuration = str(self.conanfile.settings.build_type)
        self.configurations = [
            v for v in conanfile.settings.build_type.values_range if v != "None"]
        # FIXME: Ugly way to define the output path
        self.output_path = os.getcwd()

        self.template_env = Environment(
            loader=DictLoader({
                'base_config.tpl':self.base_config_tpl,
                'config.tpl':self.config_tpl,
                'comp_config.tpl':self.comp_config_tpl,
                'target_properties.tpl':self.target_properties_tpl,

                'targets.tpl':self.targets_tpl,

                'config_version.tpl':self.config_version_tpl,

                'global_target_buildtype.tpl':self.global_target_buildtype_tpl,
                'base_target_buildtype.tpl':self.base_target_buildtype_tpl,
                'comp_target_buildtype.tpl':self.comp_target_buildtype_tpl,

                'build_modules.tpl':self.build_modules #Helper module
            }),
            autoescape=select_autoescape()
        )
        self._macros_and_functions = "\n".join([
            CMakeData.conan_message,
            CMakeData.apple_frameworks_macro,
            CMakeData.conan_package_library_targets,
        ])
        CMakeFindPackageMultiGeneratorCustom.setup_cmake_filters(self.template_env)
        if OSInfo().is_windows:
            self._vcvars_env = vcvars_dict(conanfile)
        else:
            self._vcvars_env = None
    @staticmethod
    def setup_cmake_filters(env:Environment):
        env.filters['cmake_val'] = lambda x: '${'+x+'}'
        env.filters['cmake_value'] = lambda x: '${'+x+'}'
        env.filters['cmake_pathsjoin'] = CmakeFilters.cmake_pathsjoin
        env.filters['cmake_flagsjoin'] = CmakeFilters.cmake_flagsjoin
        env.filters['cmake_definesjoin'] = CmakeFilters.cmake_definesjoin
        env.filters['cmake_pathsjoinsingle'] = CmakeFilters.cmake_pathsjoinsingle
        env.filters['cmake_apply_filter'] = CmakeFilters.cmake_apply_filter
        env.filters['list_format'] = lambda values, formatstr: [formatstr.format(v) for v in values]
    
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
        components = super(CMakeFindPackageMultiGeneratorCustom, self)._get_components(pkg_name, cpp_info)
        ret = []
        for comp_genname, comp, comp_requires_gennames in components:
            deps_cpp_cmake = copy.deepcopy(comp)

            deps_cpp_cmake.public_deps = " ".join(
                ["{}::{}".format(*it) for it in comp_requires_gennames])
            deps_cpp_cmake.build_module_paths = cpp_info.build_modules_paths.get(self.name, [])
            import_lib_info = self.import_library_info_from_cppinfo(deps_cpp_cmake)
            deps_cpp_cmake.import_lib_info = import_lib_info
            ret.append((comp_genname, deps_cpp_cmake))
        return ret

    def deduce_windows_import_type(self, lib_path, cpp_info):
        # Enable vc
        with run_with_env(self._vcvars_env):
            process = subprocess.run(
                ['lib', '/LIST', '/NOLOGO', lib_path], capture_output=True, encoding='utf-8')
            output_dump = process.stdout
            for line in output_dump.split('\n'):
                strip_line = line.strip()
                if line.strip().endswith('.obj'):
                    return {'import_type':'STATIC', 'has_importlib':False}
                else:
                    # Heuristcally find the dll corresponding to importlib.
                    dll_to_find = strip_line
                    dll_location = None
                    for bin_dir in cpp_info.bindirs:
                        file_to_try = Path(cpp_info.rootpath) / bin_dir / dll_to_find
                        if file_to_try.exists():
                            dll_location = str(file_to_try)
                    if dll_location is None:
                        raise Exception("Could not locate dll {}. Tried: {}".format(dll_to_find,
                        ','.join([str(Path(cpp_info.rootpath)/b) for b in cpp_info.bindirs])))
                    return {'import_type':'SHARED', 'has_importlib':True, 
                        'importlib':lib_path,'dll_location':dll_location}

    def deduce_linux_import_type(self, lib_path, cpp_info):
        if lib_path.endswith('dynlib') or lib_path.endswith('so'):
            return {'import_type':'SHARED', 'has_importlib':False}
        return {'import_type':'STATIC', 'has_importlib':False}

    def deduce_import_type(self, lib_path, cpp_info):
        os_info = OSInfo()
        if os_info.is_windows:
            return self.deduce_windows_import_type(lib_path, cpp_info)
        else:
            return self.deduce_linux_import_type(lib_path, cpp_info)

    def import_library_info_from_cppinfo(self, cpp_info):
        if len(cpp_info.libs) > 0:
            lib_files = []
            for lib in cpp_info.libs:
                for libdir in cpp_info.libdirs:
                    result = glob.glob(
                        str(Path(cpp_info.rootpath) / libdir/'{}.*'.format(lib)))
                    lib_files.extend(result)
            if len(lib_files) == 1:
                return self.deduce_import_type(lib_files[0], cpp_info)
        # Default to INTERFACe if no library can be found
        return {'import_type':'INTERFACE', 'has_importlib':False}

    def _render_template_str(self, template_name, **kwargs):
        print('Acquiring {}'.format(template_name))
        template = self.template_env.get_template(template_name)
        print('Rendering {}'.format(template_name))
        # Add some defaults that we always expose
        return template.render(
                    cmake_version_check=CMakeData.cmake_version_check,
                    macros_and_functions=self._macros_and_functions,
                    **kwargs)
    
    def _render_template(self, template_name, output_name, output:dict[str,str], **kwargs):
        output[output_name] = self._render_template_str(template_name, **kwargs)

    def generate_dependency_with_components(self, printer: IndentedPrint, cpp_info, pkg: PackageSpec, bt: BuildTypeSpec, output_files: dict[str, str]):
        cpp_info = extend(cpp_info, bt.build_type.lower())

        cpp_info.import_lib_info = self.import_library_info_from_cppinfo(cpp_info)
        # Tuple of name, weird FindPackageGen object and cpp_info
        components = self._get_components_of_dependency(pkg.name, cpp_info)
        
        # Note these are in reversed order, from more dependent to less dependent
        pkg_components = " ".join(["{p}::{c}".format(p=pkg.namespace, c=comp_findname) for
                                   comp_findname, _ in reversed(components)])
        render_args = dict(pkg=pkg,
            components=components,
            build_type=bt.build_type
        )
        self._render_template('comp_target_buildtype.tpl', self._targets_filename(pkg.filename, bt.build_type.lower()), output_files,
            **render_args,
            component_vars=self.component_vars,
            pkg_components=pkg_components,
            deps=cpp_info
        )
        self._render_template('targets.tpl', self._targets_filename(pkg.filename), output_files,
            **render_args
        )
        self._render_template('comp_config.tpl', self._config_filename(pkg.filename), output_files,
            **render_args,
            pkg_public_deps=pkg.public_deps_filenames,
            configs=self.configurations
        )

    def generate_dependency_without_components(self, printer: IndentedPrint, cpp_info, pkg: PackageSpec, bt: BuildTypeSpec, output_files: dict[str, str]):
        self._render_template('config.tpl',self._config_filename(pkg.filename),output_files,
            filename=pkg.filename,
            name=pkg.findname,
            namespace=pkg.namespace,
            version=cpp_info.version,
            public_deps_filenames=pkg.public_deps_filenames
        )

        # If any config matches the build_type one, add it to the cpp_info
        dep_cpp_info = extend(cpp_info, bt.build_type.lower())

        # Get import type
        dep_cpp_info.import_lib_info = self.import_library_info_from_cppinfo(dep_cpp_info)
        
        # Targets of the package
        self._render_template('targets.tpl', self._targets_filename(pkg.filename), output_files,
            pkg=pkg,
            deps = dep_cpp_info,
            import_type=dep_cpp_info.import_lib_info
        )

        #deps = DepsCppCmake(dep_cpp_info, self.name)
        # Config for build type 
        self._render_template('base_target_buildtype.tpl',self._targets_filename(pkg.filename, self.configuration.lower()),output_files,
            component_vars =self.component_vars,
            name=pkg.findname, deps=dep_cpp_info,
            pkg=pkg,
            build_type=bt.build_type,
            deps_names=pkg.deps_names)

    def generate_dependency_files(self, printer: IndentedPrint, output_files: dict[str, str], pkg_name: str, cpp_info, buildtype_spec: BuildTypeSpec):
        self._validate_components(cpp_info)
        pkg = PackageSpec()
        pkg.name = pkg_name
        pkg.filename = self._get_filename(cpp_info)
        pkg.findname = self._get_name(cpp_info)
        pkg.namespace = pkg.findname
        pkg.version = cpp_info.version

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
        self._render_template('config_version.tpl',self._config_version_filename(pkg.filename), output_files,
            version=pkg.version
        )
        # Generate dependency files
        if not cpp_info.components:
            self.generate_dependency_without_components(
                printer, cpp_info, pkg, buildtype_spec, output_files)
        else:
            self.generate_dependency_with_components(
                printer, cpp_info, pkg, buildtype_spec, output_files)

    @property
    def content(self):
        ret = {}
        printer = IndentedPrint()
        buildtype_spec = BuildTypeSpec()
        buildtype_spec.build_type = str(self.conanfile.settings.build_type).upper()
        buildtype_spec.build_type_suffix = "_{}".format(
            self.configuration.upper()) if self.configuration else ""
        
        for pkg_name, cpp_info in self.deps_build_info.dependencies:
            self.generate_dependency_files(
                printer, ret, pkg_name, cpp_info, buildtype_spec)
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

        tmp = self._render_template_str('config.tpl',
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
    exports = ["base_config.jinja",'comp_config.jinja','config.jinja','IndentedPrint.py']
