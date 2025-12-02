import json
import logging
import os
import time
from queue import Queue
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from logging_loki import LokiQueueHandler
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

# 현재 파일(main.py)이 있는 디렉토리 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()

loki_logs_handler = LokiQueueHandler(
    Queue(-1),
    url=os.getenv("LOKI_ENDPOINT", "http://loki:3100/loki/api/v1/push"),
    tags={"application": "fastapi"},
    version="1",
)

# Custom access logger (ignore Uvicorn's default logging)
custom_logger = logging.getLogger("custom.access")
custom_logger.setLevel(logging.INFO)

# Add Loki handler (assuming `loki_logs_handler` is correctly configured)
custom_logger.addHandler(loki_logs_handler)

# 요청을 Loki로 구조화해 남기는 미들웨어
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time  # Compute response time

    log_message = (
        f'{request.client.host} - "{request.method} {request.url.path} HTTP/1.1" {response.status_code} {duration:.3f}s'
    )

    custom_logger.info(log_message)

    return response

# Prometheus 메트릭스 엔드포인트 (/metrics)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# =========================================================
# [추가] InfluxDB 설정 및 연결
# =========================================================
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "my-super-secret-auth-token") 
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "myorg")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "mybucket")

# 클라이언트 생성
influx_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

# =========================================================
# [추가] 미들웨어
# =========================================================
@app.middleware("http")
async def add_influxdb_middleware(request: Request, call_next):
    start_time = time.time()
    
    # 요청 처리
    response = await call_next(request)
    
    # 처리 시간 계산 (밀리초)
    process_time = (time.time() - start_time) * 1000
    
    # InfluxDB에 기록할 데이터 생성
    try:
        point = (
            Point("http_requests")
            .tag("method", request.method)
            .tag("endpoint", request.url.path)
            .tag("status", str(response.status_code))
            .field("duration_ms", process_time)
        )
        # 비동기적으로 쓰면 좋지만, 간단한 구현을 위해 동기식 사용
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    except Exception as e:
        # InfluxDB가 죽어있어도 앱은 죽지 않도록 로그만 출력
        print(f"InfluxDB write failed: {e}")

    return response

# To-Do 항목 모델
class TodoItem(BaseModel):
    id: int
    title: str
    description: str
    completed: bool
    priority: Literal["high", "none"] = "none"

# JSON 파일 경로
TODO_FILE = "todo.json"

# JSON 파일에서 To-Do 항목 로드
def load_todos():
    if os.path.exists(TODO_FILE):
        with open(TODO_FILE, "r") as file:
            return json.load(file)
    return []

# JSON 파일에 To-Do 항목 저장
def save_todos(todos):
    with open(TODO_FILE, "w") as file:
        json.dump(todos, file, indent=4)

# To-Do 목록 조회
@app.get("/todos", response_model=list[TodoItem])
def get_todos(user: Optional[str] = None):
    todos = load_todos()
    if user:
        return [todo for todo in todos if todo.get("description") == user]
    return todos

# 신규 To-Do 항목 추가
@app.post("/todos", response_model=TodoItem)
def create_todo(todo: TodoItem):
    todos = load_todos()
    todos.append(todo.model_dump())
    save_todos(todos)
    
    # [선택] 특정 이벤트 기록 (예: 할 일 생성 횟수)
    try:
        point = Point("business_events").tag("event", "todo_created").field("count", 1)
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    except:
        pass
        
    return todo

# To-Do 항목 수정
@app.put("/todos/{todo_id}", response_model=TodoItem)
def update_todo(todo_id: int, updated_todo: TodoItem):
    todos = load_todos()
    for todo in todos:
        if todo["id"] == todo_id:
            todo.update(updated_todo.model_dump())
            save_todos(todos)
            return updated_todo
    raise HTTPException(status_code=404, detail="To-Do item not found")

# To-Do 항목 삭제
@app.delete("/todos/{todo_id}", response_model=dict)
def delete_todo(todo_id: int):
    todos = load_todos()
    todos = [todo for todo in todos if todo["id"] != todo_id]
    save_todos(todos)
    return {"message": "To-Do item deleted"}


@app.get("/", response_class=HTMLResponse)
def read_root():
    # 절대 경로를 사용하여 파일에 접근
    html_file_path = os.path.join(BASE_DIR, "templates", "index.html")
    with open(html_file_path, "r") as file: # 66행 근처
        content = file.read()
    return HTMLResponse(content=content)
