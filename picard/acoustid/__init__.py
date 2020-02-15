# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2011 Lukáš Lalinský
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

from collections import deque
from functools import partial
import json

from PyQt5 import QtCore

from picard import (
    config,
    log,
    match_details_log,
)
from picard.acoustid.json_helpers import parse_recording
from picard.const import FPCALC_NAMES
from picard.const.sys import IS_FROZEN
from picard.util import find_executable
from picard.webservice import WebService


def get_score(node):
    try:
        return float(node.get('score', 1.0))
    except (TypeError, ValueError):
        return 1.0


class AcoustIDClient(QtCore.QObject):

    def __init__(self, acoustid_api):
        super().__init__()
        self._queue = deque()
        self._running = 0
        self._max_processes = 2
        self._acoustid_api = acoustid_api

        # The second condition is checked because in case of a packaged build of picard
        # the temp directory that pyinstaller decompresses picard into changes on every
        # launch, thus we need to ignore the existing config values.
        if not config.setting["acoustid_fpcalc"] or IS_FROZEN:
            fpcalc_path = find_executable(*FPCALC_NAMES)
            if fpcalc_path:
                config.setting["acoustid_fpcalc"] = fpcalc_path

    def init(self):
        pass

    def done(self):
        pass

    def _on_lookup_finished(self, next_func, file, document, http, error):
        doc = {}
        if error:
            mparms = {
                'error': http.errorString(),
                'body': document,
                'filename': file.filename,
            }
            log.error(
                "AcoustID: Lookup network error for '%(filename)s': %(error)r, %(body)s" %
                mparms)
            self.tagger.window.set_statusbar_message(
                N_("AcoustID lookup network error for '%(filename)s'!"),
                mparms,
                echo=None
            )
        else:
            try:
                recording_list = doc['recordings'] = []
                status = document['status']
                if status == 'ok':
                    log.debug("AcoustID Lookup successful for '%s'", file.filename)
                    results = document.get('results') or []
                    result_cnt = 0
                    for result in results:
                        result_cnt += 1
                        recordings = result.get('recordings') or []
                        max_sources = max([r.get('sources', 1) for r in recordings] + [1])
                        result_score = get_score(result)
                        recording_cnt = 0
                        for recording in recordings:
                            recording_cnt += 1
                            parsed_recording = parse_recording(recording)
                            if parsed_recording is not None:
                                # Calculate a score based on result score and sources for this
                                # recording relative to other recordings in this result
                                recording_src_score = recording.get('sources', 1) / max_sources * 100
                                parsed_recording['score'] = recording_src_score * result_score
                                parsed_recording['acoustid'] = result['id']
                                recording_list.append(parsed_recording)

                                log.debug("MATCH:AcoustID Lkp: Result#: %d, Recording#: %d, recording_score: %7.4f calc is (result_score %.5f * nbr_src_ratio: (%3d/%3d=%8.4f)), File:%s",
                                         result_cnt, recording_cnt, parsed_recording['score'], result_score, recording.get('sources', 1), max_sources, recording_src_score, file.filename)
                else:
                    mparms = {
                        'error': document['error']['message'],
                        'filename': file.filename
                    }
                    log.error(
                        "AcoustID: Lookup error for '%(filename)s': %(error)r" %
                        mparms)
                    self.tagger.window.set_statusbar_message(
                        N_("AcoustID lookup failed for '%(filename)s'!"),
                        mparms,
                        echo=None
                    )
            except (AttributeError, KeyError, TypeError) as e:
                log.error("AcoustID: Error reading response", exc_info=True)
                error = e

            match_details_log.set_file_result_json(file.filename, WebService.get_last_last_raw_reply())

        next_func(doc, http, error)

    def _lookup_fingerprint(self, next_func, filename, result=None, error=None):
        try:
            file = self.tagger.files[filename]
        except KeyError:
            # The file has been removed. do nothing
            return
        mparms = {
            'filename': file.filename
        }
        if not result:
            log.debug(
                "AcoustID: lookup returned no result for file '%(filename)s'" %
                mparms
            )
            self.tagger.window.set_statusbar_message(
                N_("AcoustID lookup returned no result for file '%(filename)s'"),
                mparms,
                echo=None
            )
            file.clear_pending()
            return
        log.debug(
            "AcoustID: looking up the fingerprint for file '%(filename)s'" %
            mparms
        )
        self.tagger.window.set_statusbar_message(
            N_("Looking up the fingerprint for file '%(filename)s' ..."),
            mparms,
            echo=None
        )
        params = dict(meta='recordings releasegroups releases tracks compress sources')
        if result[0] == 'fingerprint':
            fp_type, fingerprint, length = result
            params['fingerprint'] = fingerprint
            params['duration'] = str(length)
        else:
            fp_type, recordingid = result
            params['recordingid'] = recordingid
        self._acoustid_api.query_acoustid(partial(self._on_lookup_finished, next_func, file), **params)

    def _on_fpcalc_finished(self, next_func, file, exit_code, exit_status):
        process = self.sender()
        finished = process.property('picard_finished')
        if finished:
            return
        process.setProperty('picard_finished', True)
        result = None
        try:
            self._running -= 1
            self._run_next_task()
            if exit_code == 0 and exit_status == 0:
                output = bytes(process.readAllStandardOutput()).decode()
                jsondata = json.loads(output)
                # Use only integer part of duration, floats are not allowed in lookup
                duration = int(jsondata.get('duration'))
                fingerprint = jsondata.get('fingerprint')
                if fingerprint and duration:
                    result = 'fingerprint', fingerprint, duration
            else:
                log.error(
                    "Fingerprint calculator failed exit code = %r, exit status = %r, error = %s",
                    exit_code,
                    exit_status,
                    process.errorString())
        except (json.decoder.JSONDecodeError, UnicodeDecodeError, ValueError):
            log.error("Error reading fingerprint calculator output", exc_info=True)
        finally:
            if result and result[0] == 'fingerprint':
                fp_type, fingerprint, length = result
                file.acoustid_fingerprint = fingerprint
                file.acoustid_length = length
                self.tagger.acoustidmanager.add(file, None)
            next_func(result)

    def _on_fpcalc_error(self, next_func, filename, error):
        process = self.sender()
        finished = process.property('picard_finished')
        if finished:
            return
        process.setProperty('picard_finished', True)
        try:
            self._running -= 1
            self._run_next_task()
            log.error("Fingerprint calculator failed error = %s (%r)", process.errorString(), error)
        finally:
            next_func(None)

    def _run_next_task(self):
        try:
            file, next_func = self._queue.popleft()
        except IndexError:
            return
        fpcalc = config.setting["acoustid_fpcalc"] or "fpcalc"
        self._running += 1
        process = QtCore.QProcess(self)
        process.setProperty('picard_finished', False)
        process.finished.connect(partial(self._on_fpcalc_finished, next_func, file))
        process.error.connect(partial(self._on_fpcalc_error, next_func, file))
        process.start(fpcalc, ["-json", "-length", "120", file.filename])
        log.debug("Starting fingerprint calculator %r %r", fpcalc, file.filename)

    def analyze(self, file, next_func):
        fpcalc_next = partial(self._lookup_fingerprint, next_func, file.filename)

        fingerprint = getattr(file, 'acoustid_fingerprint', None)
        if not fingerprint and not config.setting["ignore_existing_acoustid_fingerprints"]:
            # use cached fingerprint from file metadata
            fingerprints = file.metadata.getall('acoustid_fingerprint')
            if fingerprints:
                fingerprint = fingerprints[0]

        # If the fingerprint already exists skip calling fpcalc
        if fingerprint:
            length = int(file.metadata.length / 1000)
            fpcalc_next(result=('fingerprint', fingerprint, length))
            return

        # calculate the fingerprint
        self.fingerprint(file, fpcalc_next)

    def fingerprint(self, file, next_func):
        task = (file, next_func)
        self._queue.append(task)
        if self._running < self._max_processes:
            self._run_next_task()

    def stop_analyze(self, file):
        new_queue = deque()
        for task in self._queue:
            if task[0] != file:
                new_queue.appendleft(task)
        self._queue = new_queue
