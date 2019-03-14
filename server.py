import logging
import tornado.escape
import tornado.ioloop
import tornado.options
import tornado.web
import tornado.websocket
import tornado.autoreload
import os.path
import uuid
import struct
import time
import warnings

from tornado.options import define, options
from tornado.tcpserver import TCPServer
from tornado.iostream import StreamClosedError

from ross_vis.DataModel import RossData
from ross_vis.DataCache import RossDataCache
from ross_vis.Transform import flatten, flatten_list
from ross_vis.Analytics import Analytics
from ross_vis.ProgAnalytics import StreamData

from WebSocketServer import WebSocketHandler

define("http", default=8888, help="run on the given port", type=int)
define("stream", default=8000, help="streaming on the given port", type=int)
define("appdir", default="../app/dist", help="serving app in given directory", type=str)
define("datafile", default='', help="load data from file", type=str)

warnings.filterwarnings('ignore')

class Application(tornado.web.Application):
    def __init__(self, appdir = 'app'):
        handlers = [
            (r"/", MainHandler),
            (r'/app/(.*)', tornado.web.StaticFileHandler, {'path': appdir}),
            (r"/data", AjaxGetJsonData),
            (r"/analysis/(\w+)/(\w+)", AnalysisHandler),
            (r"/websocket", WebSocketHandler)
        ]
        settings = dict(
            cookie_secret="'a6u^=-sr5ph027bg576b3rl@#^ho5p1ilm!q50h0syyiw#zjxwxy0&gq2j*(ofew0zg03c3cyfvo'",
            template_path=os.path.join(os.path.dirname(__file__), appdir),
            static_path=os.path.join(os.path.dirname(__file__), appdir+'/static'),
            xsrf_cookies=True,
        )
        super(Application, self).__init__(handlers, **settings)

class StreamServer(TCPServer):
    sources = set()
    cache = []
    cache_size = 100
    
    def set_data_handler(self, handler):
        if(callable(handler)):
            self.data_handler = handler

    async def handle_stream(self, stream, address):
        StreamServer.sources.add(self)
        while True:
            try:
                sizeBuf = await stream.read_bytes(RossData.FLATBUFFER_OFFSET_SIZE)
                size = RossData.size(sizeBuf)
                data = await stream.read_bytes(size)
                if(size == len(data)):
                    logging.info('received and processed %d bytes', size)
                else:
                    print(size, len(data))
                WebSocketHandler.cache.push(data)

            except StreamClosedError:
                logging.info('stream connection closed')
                StreamServer.sources.remove(self)
                break

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")        
        
class AjaxGetJsonData(tornado.web.RequestHandler):
    def get(self):
        schema = {k:type(v).__name__ for k,v in data[0].items()}
        self.write({
            'data': WebSocketHandler.KpData,
            'schema': schema
        })

class AnalysisHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "x-requested-with")
        self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')

    def get(self, granularity, reduction):
        metrics = self.get_arguments('metrics')
        data = WebSocketHandler.cache.export_dict('KpData')
        analysis = Analytics(data, excludes=['CommData'], index=['Peid', 'Kpid', 'RealTs', 'LastGvt', 'VirtualTs', 'KpGid', 'EventId'])
        if granularity == 'PE':
            analysis.groupby(['Peid'])
        else:
            analysis.groupby(['KpGid'])

        analysis.pca(2)
        result = analysis.dbscan().kmeans()      

        self.write({
            'data': result.data.to_dict('records'),
            'schema':result.schema
        })

def main():
    tornado.options.parse_command_line()

    if (os.path.isfile(options.datafile)):
        WebSocketHandler.cache.loadfile(options.datafile)
       # WebSocketHandler.KpData = WebSocketHandler.cache.export_dict('KpData')
        print('Test mode: loaded %d samples' % WebSocketHandler.cache.size())

    app = Application(options.appdir)
    app.listen(options.http)

    server = StreamServer()
    server.listen(options.stream)
    print("Receiving data streams on", 'localhost', options.stream)
    print("HTTP and WebSocket listening on", 'localhost', options.http)

    ross_path = '/'.join(os.path.realpath(__file__).split('/')[:-2])

    watch_paths = dict(
        client_path = os.path.join(ross_path, "ross-vis/dist/"),
        server_path=os.path.join(ross_path, "ross-vis-server")
    )

    #automatically restart server on code change.
    tornado.autoreload.start()
    for key, path in watch_paths.items():
        print("Watching {0} ({1}) for changes".format(key, path))
        for dir, _, files in os.walk(path):
            [tornado.autoreload.watch(path + '/' + f) for f in files if not f.startswith('.')]
    
    stream_objs = {}
    def process(stream, stream_count):
        metric = ['RbSec']
        granularity = 'KpGid'
        time_domain = 'LastGvt'
        algo = {
            'cpd': 'aff',
            'pca': 'prog_inc',
            'causality': 'var',
            'clustering': 'evostream',
        }  
        ret = {}                  
        for idx, metric in enumerate(metric):
            print('Calculating results for {0}'.format(metric))
            if stream_count == 0: 
                stream_data = StreamData(stream, granularity, metric, time_domain)
                stream_objs[metric] = stream_data
                ret[metric] = stream_data.format()
                ret['data'] = [{}, {}]
            elif stream_count < 2: 
                stream_obj = stream_objs[metric]
                stream_data = stream_obj.update(stream)
                ret[metric] = stream_data.format()
                ret['data'] = stream_obj.comm_data()
            else:
                stream_obj = stream_objs[metric]
                stream_data = stream_obj.update(stream)
                ret[metric] = stream_obj.run_methods(stream_data, algo)
                ret['data'] = stream_obj.comm_data()
        return ret 

    data_attribute = 'KpData'
    max_stream_count = 100
    for stream_count in range(0, max_stream_count):
        rd = RossData([data_attribute])
        sample = WebSocketHandler.cache.data[stream_count]
        stream = flatten(rd.fetch(sample))
        res = process(stream, stream_count)

    


    tornado.ioloop.IOLoop.current().start()    


if __name__ == "__main__":
    main()
