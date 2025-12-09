"""
UNIETAXI - Versión PRO (corregida y estable)
Archivo: UNIETAXI_webapp_pro.py
"""

from flask import Flask, jsonify, render_template_string, request, send_file
import threading, time, random, math, queue, io, os
from dataclasses import dataclass, field
from typing import Tuple, Dict, List, Optional

# Intento de importar reportlab (opcional)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ================ CONFIG ================
ADMIN_USER = 'admin'
ADMIN_PASS = 'admin123'
DEFAULT_TAXIS = 5
DEFAULT_CLIENTS = 10
MATCH_RADIUS = 2.0
SERVICE_FEE_RATE = 0.20
MADRID_MIN_X, MADRID_MAX_X = 40.37, 40.50
MADRID_MIN_Y, MADRID_MAX_Y = -3.80, -3.63
DATA_DIR = 'unietaxi_reports'
os.makedirs(DATA_DIR, exist_ok=True)

# ================ MODELOS ================
@dataclass
class Taxi:
    taxi_id: int
    plate: str
    location: Tuple[float, float]
    rating: float = 4.5
    available: bool = True
    earnings: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    def distance_to(self, pos: Tuple[float, float]):
        return math.dist(self.location, pos)

@dataclass
class Client:
    client_id: int
    name: str
    location: Tuple[float, float]

@dataclass
class RequestObj:
    request_id: int
    client: Client
    origin: Tuple[float, float]
    destination: Tuple[float, float]
    timestamp: float
    assigned_taxi: Optional[int] = None
    completed: bool = False
    fare: float = 0.0

# ================ SISTEMA ================
class CentralSystem:
    def __init__(self):
        self.taxis: Dict[int, Taxi] = {}
        self.taxis_lock = threading.Lock()
        self.requests = queue.Queue()
        self.requests_index: Dict[int, RequestObj] = {}
        self.requests_lock = threading.Lock()
        self.request_counter = 0
        self.binary_semaphore = threading.Semaphore(1)
        self.completed_services: List[RequestObj] = []
        self.completed_lock = threading.Lock()
        self.company_monthly_profit = 0.0
        self.profit_lock = threading.Lock()
        self.new_request_condition = threading.Condition()
        self.adjudicator_thread = threading.Thread(target=self._adjudicator, daemon=True)
        self.running = False
        self.time_series = {'timestamps': [], 'completed': [], 'profit': []}
        self.ts_lock = threading.Lock()

    def register_taxi(self, taxi: Taxi):
        with self.taxis_lock:
            self.taxis[taxi.taxi_id] = taxi

    def create_client_request(self, client: Client, origin: Tuple[float,float], dest: Tuple[float,float]) -> int:
        with self.requests_lock:
            self.request_counter += 1
            rid = self.request_counter
            r = RequestObj(request_id=rid, client=client, origin=origin, destination=dest, timestamp=time.time())
            self.requests.put(r)
            self.requests_index[rid] = r
        with self.new_request_condition:
            self.new_request_condition.notify()
        return rid

    def start(self):
        self.running = True
        if not self.adjudicator_thread.is_alive():
            self.adjudicator_thread = threading.Thread(target=self._adjudicator, daemon=True)
            self.adjudicator_thread.start()

    def stop(self):
        self.running = False
        with self.new_request_condition:
            self.new_request_condition.notify_all()
        time.sleep(0.2)

    def _adjudicator(self):
        while self.running:
            with self.new_request_condition:
                while self.requests.empty() and self.running:
                    self.new_request_condition.wait(timeout=1)
                if not self.running:
                    break
                try:
                    req: RequestObj = self.requests.get_nowait()
                except queue.Empty:
                    continue

            acquired = self.binary_semaphore.acquire(timeout=3)
            if not acquired:
                self.requests.put(req)
                continue
            try:
                assigned = self._assign_taxi(req)
            finally:
                self.binary_semaphore.release()
            if not assigned:
                time.sleep(0.3)
                self.requests.put(req)

    def _assign_taxi(self, req: RequestObj) -> bool:
        candidates: List[tuple] = []
        with self.taxis_lock:
            for taxi in self.taxis.values():
                with taxi.lock:
                    if not taxi.available:
                        continue
                    d = taxi.distance_to(req.origin)
                    if d <= MATCH_RADIUS:
                        candidates.append((d, taxi))
        if not candidates:
            return False
        candidates.sort(key=lambda x: (x[0], -x[1].rating))
        chosen = candidates[0][1]
        with chosen.lock:
            if not chosen.available:
                return False
            chosen.available = False
        req.assigned_taxi = chosen.taxi_id
        threading.Thread(target=self._service, args=(req, chosen), daemon=True).start()
        return True

    def _service(self, req: RequestObj, taxi: Taxi):
        def move_smooth(start, end, steps=20, delay=0.12):
            lat1, lon1 = start; lat2, lon2 = end
            dlat = (lat2 - lat1) / steps
            dlon = (lon2 - lon1) / steps
            for i in range(steps):
                with taxi.lock:
                    taxi.location = (lat1 + dlat*(i+1), lon1 + dlon*(i+1))
                time.sleep(delay)
        with taxi.lock:
            start_pos = taxi.location
        move_smooth(start_pos, req.origin, steps=15, delay=0.08)
        move_smooth(req.origin, req.destination, steps=25, delay=0.08)
        dist = math.dist(req.origin, req.destination)
        fare = round(dist * 1.6 + 2.0, 2)
        with taxi.lock:
            taxi.location = req.destination
            taxi.available = True
            taxi.earnings += fare * (1 - SERVICE_FEE_RATE)
            taxi.rating = min(5.0, round((taxi.rating + random.uniform(3.7, 5.0)) / 2.0, 2))
        req.fare = fare
        req.completed = True
        with self.completed_lock:
            self.completed_services.append(req)
        with self.profit_lock:
            self.company_monthly_profit += fare * SERVICE_FEE_RATE
        ts = time.time()
        with self.ts_lock:
            self.time_series['timestamps'].append(ts)
            self.time_series['completed'].append(len(self.completed_services))
            self.time_series['profit'].append(self.company_monthly_profit)

    def get_snapshot(self):
        with self.taxis_lock, self.completed_lock, self.profit_lock, self.requests_lock:
            taxis = {tid: {'loc': t.location, 'rating': t.rating, 'avail': t.available, 'earnings': t.earnings} for tid, t in self.taxis.items()}
            pending = [ {'rid': r.request_id, 'client': r.client.name, 'origin': r.origin, 'destination': r.destination} for r in self.requests_index.values() if not r.completed and r.assigned_taxi is None ]
            with self.ts_lock:
                ts_copy = {k: list(v) for k, v in self.time_series.items()}
            return {'running': self.running, 'taxis': taxis, 'pending': pending, 'completed': len(self.completed_services), 'profit': self.company_monthly_profit, 'timeseries': ts_copy}

    def get_history(self):
        with self.completed_lock:
            return [{'rid': r.request_id, 'client': r.client.name, 'taxi': r.assigned_taxi, 'fare': r.fare} for r in self.completed_services]

# ================ UTILIDADES ================
def rand_madrid_point():
    return (round(random.uniform(MADRID_MIN_X, MADRID_MAX_X), 6), round(random.uniform(MADRID_MIN_Y, MADRID_MAX_Y), 6))

street_cache: Dict[str, Tuple[float,float]] = {}
def geocode_street(street: str) -> Tuple[float,float]:
    key = street.strip().lower()
    if key in street_cache:
        return street_cache[key]
    h = abs(hash(key))
    rx = MADRID_MIN_X + (h % 1000) / 1000.0 * (MADRID_MAX_X - MADRID_MIN_X)
    ry = MADRID_MIN_Y + ((h // 1000) % 1000) / 1000.0 * (MADRID_MAX_Y - MADRID_MIN_Y)
    pt = (round(rx, 6), round(ry, 6))
    street_cache[key] = pt
    return pt

# ================ FLASK APP ================
app = Flask(__name__)
central: Optional[CentralSystem] = None
client_threads: List[threading.Thread] = []
next_client_id = 1000
sessions: Dict[str, dict] = {}

# ----------------- HTML -----------------
HTML = r""" """ + open(__file__).read().split('HTML = r"""')[1].split('"""')[0] + r""" """

# ----------------- ROUTES -----------------
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
def start_sim():
    global central, client_threads, next_client_id
    if central and central.running:
        return jsonify({'ok': True})
    central = CentralSystem()
    
    # crear taxis
    taxis = [Taxi(i, f"TAXI-{100+i}", rand_madrid_point()) for i in range(1, DEFAULT_TAXIS+1)]
    for t in taxis: central.register_taxi(t)

    # crear clientes automáticos
    def client_auto_behavior(client: Client):
        global central
        while central and central.running:
            newloc = rand_madrid_point()
            client.location = newloc
            dest = rand_madrid_point()
            try:
                central.create_client_request(client, newloc, dest)
            except:
                pass
            time.sleep(random.uniform(10,20))

    clients = [Client(i, f"Cliente-{i}", rand_madrid_point()) for i in range(1, DEFAULT_CLIENTS+1)]
    client_threads = [threading.Thread(target=client_auto_behavior, args=(c,), daemon=True) for c in clients]
    for ct in client_threads: ct.start()

    central.start()
    next_client_id = 1000
    return jsonify({'ok': True})

@app.route('/stop', methods=['POST'])
def stop_sim():
    global central, client_threads
    if central:
        central.stop()
        for ct in client_threads:
            try: ct.join(0.01)
            except: pass
        central = None; client_threads = []
    return jsonify({'ok': True})

@app.route('/status')
def status():
    if not central:
        return jsonify({'running': False, 'taxis': {}, 'pending': [], 'completed': 0, 'profit': 0.0, 'timeseries': {'timestamps': [], 'completed': [], 'profit': []}})
    snap = central.get_snapshot()
    return jsonify(snap)

@app.route('/history')
def history():
    if not central: return jsonify([])
    return jsonify(central.get_history())

@app.route('/request', methods=['POST'])
def create_request():
    global central, next_client_id
    if not central or not central.running:
        return jsonify({'error': 'Sistema no está en ejecución'})
    data = request.get_json() or {}
    name = data.get('name', 'Anonimo')
    origin = geocode_street(data.get('origin', ''))
    dest = geocode_street(data.get('destination', ''))
    client = Client(next_client_id, name, origin)
    next_client_id += 1
    rid = central.create_client_request(client, origin, dest)
    return jsonify({'rid': rid})

@app.route('/admin_login', methods=['POST'])
def admin_login():
    data = request.get_json() or {}
    user = data.get('user'); pwd = data.get('pass')
    if user == ADMIN_USER and pwd == ADMIN_PASS:
        token = str(time.time())
        sessions[token] = {'user': user, 'login': time.time()}
        return jsonify({'ok': True, 'token': token})
    return jsonify({'ok': False})

@app.route('/export_report')
def export_report():
    if not REPORTLAB_AVAILABLE:
        return jsonify({'error': 'reportlab not installed'})
    if not central:
        return jsonify({'error': 'no_running'})
    timestamp = int(time.time())
    filename = os.path.join(DATA_DIR, f'unietaxi_report_{timestamp}.pdf')
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    c.setFont('Helvetica-Bold', 16); c.drawString(40, height-60, 'UNIETAXI - Informe de Simulación')
    c.setFont('Helvetica', 10); c.drawString(40, height-80, time.ctime())
    snap = central.get_snapshot()
    c.drawString(40, height-110, f"Taxis registrados: {len(snap['taxis'])}")
    c.drawString(40, height-125, f"Servicios completados: {snap['completed']}")
    c.drawString(40, height-140, f"Ganancia empresa: S/{snap['profit']:.2f}")
    y = height-170
    c.setFont('Helvetica-Bold', 12); c.drawString(40, y, 'Resumen por taxi:'); y -= 18; c.setFont('Helvetica', 9)
    for tid, t in snap['taxis'].items():
        c.drawString(48, y, f"Taxi {tid} - loc: {tuple(round(x,4) for x in t['loc'])} - rating: {t['rating']} - earnings: S/{t['earnings']:.2f}")
        y -= 14
        if y < 80:
            c.showPage(); y = height-60
    c.showPage(); c.save()
    buffer.seek(0)
    with open(filename, 'wb') as f: f.write(buffer.read())
    return jsonify({'url': f'/download/{os.path.basename(filename)}'})

@app.route('/download/<path:fn>')
def download(fn):
    path = os.path.join(DATA_DIR, fn)
    if not os.path.exists(path):
        return 'Not found', 404
    return send_file(path, as_attachment=True)

if __name__ == '__main__':
    print('UNIETAXI PRO (corregido) running at http://127.0.0.1:5000')
    app.run(debug=False)
