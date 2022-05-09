
class IndentedPrint:
    def __init__(self):
        self._indent = 0
        self.tabs = ''

    def indent(self):
        self._indent += 1
        self.tabs = self.tabs + '\t'

    def dedent(self):
        self._indent -= 1
        self.tabs = self.tabs[0:-1]

    def print(self, val):
        if isinstance(val, str):
            print(self.tabs + val)
        else:
            print(self.tabs + str(val))

    def __call__(self, val):
        self.print(val)
