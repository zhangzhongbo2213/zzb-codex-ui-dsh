import subprocess, threading, queue, json, time

class Core:
    def __init__(self, binary, cwd):
        self.proc = subprocess.Popen([str(binary)], cwd=str(cwd), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.queue = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
                if message.get('id'):
                    self.queue.put(message)
            except ValueError:
                pass
        self.queue.put({'_closed': True})

    def call(self, method, data='', timeout=15):
        self.counter += 1
        ident = str(self.counter)
        self.proc.stdin.write(json.dumps({'id': ident, 'method': method, 'data': data}, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            response = self.queue.get(timeout=max(.01, end - time.monotonic()))
            if response.get('_closed'):
                raise RuntimeError('Proxy core exited')
            if response.get('id') == ident:
                return response
        raise TimeoutError(method)

    def close(self):
        try:
            self.proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            self.proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
