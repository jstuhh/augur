#spdx-license-identifier: mit
import os
import sys
import json
import re
import cgi
from flask import Flask, request, Response, send_from_directory
from flask_cors import CORS
import pandas as pd
import augur
from augur.util import annotate, metrics, determineFrontendStatus, writeMetadata
from augur.routes import create_all_routes

sys.path.append('..')

AUGUR_API_VERSION = 'api/unstable'

class Server(object):
    def __init__(self):
        # Create Flask application
        self.app = Flask(__name__)
        self.api_version = AUGUR_API_VERSION
        app = self.app
        CORS(app)

        # Create Augur application
        self.augur_app = augur.Application()
        augur_app = self.augur_app

        # Initialize cache
        expire = int(augur_app.read_config('Server', 'cache_expire', 'AUGUR_CACHE_EXPIRE', 3600))
        self.cache = augur_app.cache.get_cache('server', expire=expire)
        self.cache.clear()

        create_all_routes(self)

        #####################################
        ###          UTILITY              ###
        #####################################
        
        @app.route('/{}/'.format(self.api_version))
        def status():
            status = {
                'status': 'OK',
                'avaliable_metrics': metrics
            }
            json = self.transform(status)
            return Response(response=json,
                            status=200,
                            mimetype="application/json")

        """
        @api {post} /batch Batch Requests
        @apiName Batch
        @apiGroup Batch
        @apiDescription Returns results of batch requests
        POST JSON of api requests
        """
        @app.route('/{}/batch'.format(self.api_version), methods=['GET', 'POST'])
        def batch():
            """
            Execute multiple requests, submitted as a batch.
            :statuscode 207: Multi status
            """

            """
            to have on future batch request for each individual chart:

            - timeseries/metric
            - props that are in current card files (title)
            - do any of these things act like the vuex states?

            - what would singular card(dashboard) look like now?


            """
            if request.method == 'GET':
                """this will return sensible defaults in the future"""
                return app.make_response('{"status": "501", "response": "Defaults for batch requests not implemented. Please POST a JSON array of requests to this endpoint for now."}')

            try:
                requests = json.loads(request.data)
            except ValueError as e:
                request.abort(400)

            responses = []

            for index, req in enumerate(requests):


                method = req['method']
                path = req['path']
                body = req.get('body', None)

                try:

                    with app.app_context():
                        with app.test_request_context(path,
                                                      method=method,
                                                      data=body):
                            try:
                                # Can modify flask.g here without affecting
                                # flask.g of the root request for the batch

                                # Pre process Request
                                rv = app.preprocess_request()

                                if rv is None:
                                    # Main Dispatch
                                    rv = app.dispatch_request()

                            except Exception as e:
                                rv = app.handle_user_exception(e)

                            response = app.make_response(rv)

                            # Post process Request
                            response = app.process_response(response)

                    # Response is a Flask response object.
                    # _read_response(response) reads response.response
                    # and returns a string. If your endpoints return JSON object,
                    # this string would be the response as a JSON string.
                    responses.append({
                        "path": path,
                        "status": response.status_code,
                        "response": str(response.get_data(), 'utf8'),
                    })

                except Exception as e:

                    responses.append({
                        "path": path,
                        "status": 500,
                        "response": str(e)
                    })


            return Response(response=json.dumps(responses),
                            status=207,
                            mimetype="application/json")


    def transform(self, data, orient='records', 
        group_by=None, on=None, aggregate='sum', resample=None, date_col='date'):

        if orient is None:
            orient = 'records'

        result = ''

        if hasattr(data, 'to_json'):
            if group_by is not None:
                data = data.group_by(group_by).aggregate(aggregate)
            if resample is not None:
                data['idx'] = pd.to_datetime(data[date_col])
                data = data.set_index('idx')
                data = data.resample(resample).aggregate(aggregate)
                data['date'] = data.index
            result = data.to_json(orient=orient, date_format='iso', date_unit='ms')
        else:
            try:
                result = json.dumps(data)
            except:
                result = data

        return result

    def flaskify(self, func, cache=True):
        """
        Simplifies API endpoints that just accept owner and repo,
        transforms them and spits them out
        """
        if cache:
            def generated_function(*args, **kwargs):
                def heavy_lifting():
                    return self.transform(func(*args, **kwargs), **request.args.to_dict())
                body = self.cache.get(key=str(request.url), createfunc=heavy_lifting)
                return Response(response=body,
                                status=200,
                                mimetype="application/json")
            generated_function.__name__ = func.__name__
            return generated_function
        else:
            def generated_function(*args, **kwargs):
                kwargs.update(request.args.to_dict())
                return Response(response=self.transform(func(*args, **kwargs)),
                                status=200,
                                mimetype="application/json")
            generated_function.__name__ = func.__name__
            return generated_function

    def addMetric(self, function, endpoint, cache=True, **kwargs):
        """Simplifies adding routes that only accept owner/repo"""
        endpoint = '/{}/<owner>/<repo>/{}'.format(self.api_version, endpoint)
        self.app.route(endpoint)(self.flaskify(function, cache=cache))
        self.updateMetricMetadata(function, endpoint, **kwargs)

    def addGitMetric(self, function, endpoint, cache=True):
        """Simplifies adding routes that accept"""
        endpoint = '/{}/git/{}/<path:repo_url>/'.format(self.api_version, endpoint)
        self.app.route(endpoint)(self.flaskify(function, cache=cache))
        self.updateMetricMetadata(function, endpoint=endpoint, metric_type='git')

    def addTimeseries(self, function, endpoint):
        """
        Simplifies adding routes that accept owner/repo and return timeseries
        :param app:       Flask app
        :param function:  Function from a datasource to add
        :param endpoint:  GET endpoint to generate
        """
        self.addMetric(function, 'timeseries/{}'.format(endpoint), metric_type='timeseries')

    def updateMetricMetadata(self, function, endpoint, **kwargs):
        # God forgive me
        #
        # Get the unbound function from the bound function's class so that we can modify metadata
        # across instances of that class.
        real_func = getattr(function.__self__.__class__, function.__name__)
        tag = re.sub("_", "-", function.__name__).lower()
        frontend_status = ''
        metric_name = re.sub('_', ' ', function.__name__).title()
        annotate(metric_name=metric_name, endpoint=endpoint, escaped_endpoint=cgi.escape(endpoint), source=function.__self__.__class__.__name__, tag=tag, **kwargs)(real_func)
        writeMetadata(metrics)

def run():
    server = Server()
    host = server.augur_app.read_config('Server', 'host', 'AUGUR_HOST', '0.0.0.0')
    port = server.augur_app.read_config('Server', 'port', 'AUGUR_PORT', '5000')
    Server().app.run(host=host, port=int(port))

wsgi_app = None
def wsgi(env, start_response):
    global wsgi_app
    if (wsgi_app is None):
        app_instance = Server()
        wsgi_app = app_instance.app
    return wsgi_app(env, start_response)

if __name__ == "__main__":
    run()