#
# Copyright (C) 2026 city_notification_tracker contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import os
import sqlite3

from flask import Flask, render_template

app = Flask(__name__)

DATA_DIR = os.environ.get('DATA_DIR', '/app/data')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:8000')


@app.route('/')
def index():
    # Hello world. Reports whether the persistence volume is mounted and
    # writable, without creating a database file (the real schema comes later).
    return render_template(
        'index.html',
        base_url=BASE_URL,
        data_dir=DATA_DIR,
        data_dir_exists=os.path.isdir(DATA_DIR),
        data_dir_writable=os.access(DATA_DIR, os.W_OK),
        sqlite_version=sqlite3.sqlite_version,
    )


@app.route('/healthz')
def healthz():
    return 'ok\n', 200, {'Content-Type': 'text/plain'}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True)
