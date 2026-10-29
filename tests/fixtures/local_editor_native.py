"""Tiny HTTP native-process stand-in. Contains no model, network download or GPU work."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int)
    parser.add_argument('--alias')
    parser.add_argument('--api-key')
    args, _ = parser.parse_known_args()
    if os.environ.get('WEZZA_TEST_PID_FILE'):
        Path(os.environ['WEZZA_TEST_PID_FILE']).write_text(str(os.getpid()))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply({'data':[{'id':args.alias}]})

        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer '+os.environ['LLAMA_API_KEY']:
                self.send_error(403)
                return
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/apply-template':
                self.reply({'prompt':json.dumps(value['messages'])+'<think>\n\n</think>\n\n'})
            elif self.path == '/tokenize':
                self.reply({'tokens':[1]*20})
            else:
                user = value['messages'][1]['content']
                if user == 'stall':
                    time.sleep(30)
                self.reply({'model':args.alias,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"edited_text":"Ready."}'}}]})

    HTTPServer(('127.0.0.1',args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
