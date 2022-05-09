import subprocess
from pathlib import Path
from contextlib import contextmanager
import glob

from conans import ConanFile
from conans.client.tools.oss import OSInfo
from conans.client.tools.win import vcvars_dict, environment_append

@contextmanager
def run_with_env(env_dict):
    with environment_append(env_dict):
        yield

class ImportLibraryTypeDeduction:
    def __init__(self,conanfile: ConanFile):
        if OSInfo().is_windows:
            self._vcvars_env = vcvars_dict(conanfile)
        else:
            self._vcvars_env = None
    
    @staticmethod
    def get_dll_location(dll_to_find:str, cpp_info)-> str:
        dll_location = None
        # Search all associated binary dirs for the dll
        for bin_dir in cpp_info.bindirs:
            file_to_try = Path(cpp_info.rootpath) / bin_dir / dll_to_find
            if file_to_try.exists():
                dll_location = str(file_to_try)
        if dll_location is None:
            raise Exception("Could not locate dll {}. Tried: {}".format(dll_to_find,
            ','.join([str(Path(cpp_info.rootpath)/b) for b in cpp_info.bindirs])))
        return dll_location

    def deduce_windows_import_type(self, lib_path, cpp_info):
        # Enable vc
        first_line = ''
        with run_with_env(self._vcvars_env):
            process = subprocess.run(
                ['lib', '/LIST', '/NOLOGO', lib_path], capture_output=True, encoding='utf-8')
            output_dump = process.stdout
            for line in output_dump.split('\n'):
                first_line = line.strip()
        if first_line.strip().endswith('.obj'):
            return {'import_type':'STATIC', 'has_importlib':False}
        else:
            # Heuristcally find the dll corresponding to importlib.
            dll_to_find = first_line.strip()
            dll_location = ImportLibraryTypeDeduction.get_dll_location(dll_to_find, cpp_info)
            return {'import_type':'SHARED', 'has_importlib':True, 
                'importlib':lib_path,'dll_location':dll_location}

    def deduce_linux_import_type(self, lib_path, cpp_info):
        if lib_path.endswith('dynlib') or lib_path.endswith('so'):
            return {'import_type':'SHARED', 'has_importlib':False}
        return {'import_type':'STATIC', 'has_importlib':False}

    def _deduce_import_type(self, lib_path, cpp_info):
        os_info = OSInfo()
        if os_info.is_windows:
            return self.deduce_windows_import_type(lib_path, cpp_info)
        else:
            return self.deduce_linux_import_type(lib_path, cpp_info)

    def import_library_info_from_cppinfo(self, cpp_info):
        if len(cpp_info.libs) > 0:
            lib_files = []
            # Collect library files
            for lib in cpp_info.libs:
                for libdir in cpp_info.libdirs:
                    result = glob.glob(
                        str(Path(cpp_info.rootpath) / libdir/'{}.*'.format(lib)))
                    lib_files.extend(result)
            if len(lib_files) == 1:
                return self._deduce_import_type(lib_files[0], cpp_info)
            else:
                raise Exception("Not sure how to handle multiple files")
        # Default to INTERFACe if no library can be found
        return {'import_type':'INTERFACE', 'has_importlib':False}
