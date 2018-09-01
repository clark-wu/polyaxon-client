# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import json
import os
import requests
import tarfile
import websocket

from clint.textui import progress
from clint.textui.progress import Bar

from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from polyaxon_client.exceptions import ERRORS_MAPPING, AuthenticationError, PolyaxonShouldExitError
from polyaxon_client.logger import logger
from polyaxon_client.schemas.utils import to_list


class Transport(object):
    """HTTP operations transport."""
    TIME_OUT = 25

    def __init__(self, token=None, authentication_type='token', reraise=False):
        self.authentication_type = authentication_type
        self.token = token
        self.reraise = reraise

    @staticmethod
    def create_progress_callback(encoder):
        encoder_len = encoder.len
        progress_bar = Bar(expected_size=encoder_len, filled_char='=')

        def callback(monitor):
            progress_bar.show(monitor.bytes_read)

        return callback, progress_bar

    @staticmethod
    def format_sizeof(num, suffix='B'):
        """
        Print in human friendly format
        """
        for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
            if abs(num) < 1024.0:
                return "%3.1f%s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f%s%s" % (num, 'Yi', suffix)

    def _get_headers(self, headers=None):
        request_headers = headers or {}
        # Auth headers if access_token is present
        if self.token:
            request_headers.update({"Authorization": "{} {}".format(self.authentication_type,
                                                                    self.token)})
        return request_headers

    def request(self,
                method,
                url,
                params=None,
                data=None,
                files=None,
                json=None,  # noqa
                timeout=TIME_OUT,
                headers=None):
        """Send a request with the given data as json to the given URL.

        Args:
            method: HTTP method
            url: The url to send the request to.
            params: Dictionary of values to format url.
            data:
            files:
            json:
            timeout:
            headers: extra headers to add to the request.

        Returns:
            Request response if successful, Exception otherwise.

        Raises:
            PolyaxonHTTPError or one of its subclasses.
        """
        logger.debug("Starting request to url: %s with params: %s, data: %s",
                     url, params, data)

        request_headers = self._get_headers(headers=headers)

        try:
            response = requests.request(method,
                                        url,
                                        params=params,
                                        data=data,
                                        json=json,
                                        headers=request_headers,
                                        files=files,
                                        timeout=timeout)
        except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as exception:
            try:
                logger.debug("Exception: %s", exception, exc_info=True)
            except TypeError:
                pass
            raise PolyaxonShouldExitError(
                "Cannot connect to the Polyaxon server on `{}`.\n"
                "Check your host and ports configuration and your internet connection.".format(url))

        logger.debug("Response Content: %s, Headers: %s", response.content, response.headers)
        self.check_response_status(response, url)
        return response

    def upload(self, url, files, files_size, params=None, json_data=None, timeout=3600):
        if files_size > self.MAX_UPLOAD_SIZE:
            raise PolyaxonShouldExitError(
                "Files too large to sync, please keep it under {}.\n"
                "If you have data files in the current directory, "
                "please add them directly to your data volume, or upload them "
                "separately using `polyxon data` command and remove them from here.\n".format(
                    self.format_sizeof(self.MAX_UPLOAD_SIZE)))

        files = to_list(files)
        if json_data:
            files.append(('json', json.dumps(json_data)))

        multipart_encoder = MultipartEncoder(
            fields=files
        )

        # Attach progress bar
        progress_callback, progress_bar = self.create_progress_callback(multipart_encoder)
        multipart_encoder_monitor = MultipartEncoderMonitor(multipart_encoder, progress_callback)
        try:
            response = self.put(url=url,
                                params=params,
                                data=multipart_encoder_monitor,
                                headers={"Content-Type": multipart_encoder.content_type},
                                timeout=timeout)
        finally:
            # always make sure we clear the console
            progress_bar.done()

        return response

    def download(self, url, filename, headers=None, timeout=TIME_OUT):
        """
        Download the file from the given url at the current path
        """
        logger.debug("Downloading files from url: %s", url)

        request_headers = self._get_headers(headers=headers)

        try:
            response = requests.get(url,
                                    headers=request_headers,
                                    timeout=timeout,
                                    stream=True)
            self.check_response_status(response, url)
            with open(filename, 'wb') as f:
                # chunk mode response doesn't have content-length so we are
                # using a custom header here
                content_length = response.headers.get('x-polyaxon-content-length')
                if not content_length:
                    content_length = response.headers.get('content-length')
                if content_length:
                    for chunk in progress.bar(response.iter_content(chunk_size=1024),
                                              expected_size=(int(content_length) / 1024) + 1):
                        if chunk:
                            f.write(chunk)
                else:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
            return filename
        except requests.exceptions.ConnectionError as exception:
            try:
                logger.debug("Exception: %s", exception)
            except TypeError:
                pass

            raise PolyaxonShouldExitError(
                "Cannot connect to the Polyaxon server on `{}`.\n"
                "Check your host and ports configuration and your internet connection.".format(url))

    def download_tar(self, url, untar=True, delete_tar=False):
        """
        Download and optionally untar the tar file from the given url
        """
        try:
            logger.info("Downloading the tar file to the current directory ...")
            filename = self.download(url=url, filename='output.tar')
            if filename and untar:
                logger.info("Untarring the contents of the file ...")
                tar = tarfile.open(filename)
                tar.extractall()
                tar.close()
            if delete_tar:
                logger.info("Cleaning up the tar file ...")
                os.remove(filename)
            return filename
        except ERRORS_MAPPING['base'] as e:
            logger.info("Download URL ERROR! %s", e.message)
            return False

    def check_response_status(self, response, endpoint):
        """Check if response is successful. Else raise Exception."""

        if 200 <= response.status_code < 300:
            return response

        try:
            logger.error(
                "Request to %s failed with status code %s. \n"
                "Reason: %s", endpoint, response.status_code, response.text)
        except TypeError:
            logger.error("Request to %s failed with status code", endpoint)

        exception = ERRORS_MAPPING.get(response.status_code, ERRORS_MAPPING['http'])
        raise exception(endpoint=endpoint,
                        response=response,
                        message=response.text,
                        status_code=response.status_code)

    def handle_exception(self, e, log_message=None):
        logger.info("%s: %s", log_message, e.message)

        if self.reraise:
            raise e

        if isinstance(e, AuthenticationError):
            # exit now since there is nothing we can do without login
            raise e

    def get(self,
            url,
            params=None,
            data=None,
            files=None,
            json_data=None,
            timeout=TIME_OUT,
            headers=None):
        """Call request with a get."""
        return self.request('GET',
                            url=url,
                            params=params,
                            data=data,
                            files=files,
                            json=json_data,
                            timeout=timeout,
                            headers=headers)

    def post(self,
             url,
             params=None,
             data=None,
             files=None,
             json_data=None,
             timeout=TIME_OUT,
             headers=None):
        """Call request with a post."""
        return self.request('POST',
                            url=url,
                            params=params,
                            data=data,
                            files=files,
                            json=json_data,
                            timeout=timeout,
                            headers=headers)

    def patch(self,
              url,
              params=None,
              data=None,
              files=None,
              json_data=None,
              timeout=TIME_OUT,
              headers=None):
        """Call request with a patch."""
        return self.request('PATCH',
                            url=url,
                            params=params,
                            data=data,
                            files=files,
                            json=json_data,
                            timeout=timeout,
                            headers=headers)

    def delete(self,
               url,
               params=None,
               data=None,
               files=None,
               json_data=None,
               timeout=TIME_OUT,
               headers=None):
        """Call request with a delete."""
        return self.request('DELETE',
                            url=url,
                            params=params,
                            data=data,
                            files=files,
                            json=json_data,
                            timeout=timeout,
                            headers=headers)

    def put(self,
            url,
            params=None,
            data=None,
            files=None,
            json_data=None,
            timeout=TIME_OUT,
            headers=None):
        """Call request with a put."""
        return self.request('PUT',
                            url=url,
                            params=params,
                            data=data,
                            files=files,
                            json=json_data,
                            timeout=timeout,
                            headers=headers)

    def socket(self, url, message_handler, headers=None):
        wes = websocket.WebSocketApp(
            url,
            on_message=lambda ws, message: self._on_message(message_handler, message),
            on_error=self._on_error,
            on_close=self._on_close,
            header=self._get_headers(headers)
        )
        wes.run_forever()

    def _on_message(self, message_handler, message):
        message_handler(json.loads(message))

    def _on_error(self, ws, error):
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            logger.info('Quitting... The session will be running in the background.')
        else:
            logger.debug('Termination cause: %s', error)
            logger.debug('Session disconnected.')

    def _on_close(self, ws):
        logger.info('Session ended')