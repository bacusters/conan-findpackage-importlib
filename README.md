# Installing
First export the package to the local cache.

``conan export . myuser/testing``

Then, build the module using

``conan install customcmakegen@myuser/testing --build=customcmakegen``