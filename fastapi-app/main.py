from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import json
import os
import time
from typing import Literal, Optional
from prometheus_fastapi_instrumentator import Instrumentator

# [추가됨] InfluxDB 관련 라이브러리 임포트
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# 현재 파일(main.py)이 있는 디렉토리 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()

# Prometheus 메트릭스 엔드포인트 (/metrics)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# =========================================================
# [추가됨] InfluxDB 설정 및 연결
# =========================================================
# Docker Compose에서 설정한 값과 일치해야 합니다.
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://influxdb:8086")
# 주의: 토큰은 InfluxDB 초기화 시 설정한 토큰이나 웹 UI에서 발급받은 토큰을 넣어야 합니다.
# 보안을 위해 환경변수로 받는 것이 좋지만, 테스트를 위해 여기에 직접 넣으셔도 됩니다.
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "my-super-secret-auth-token") 
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "myorg")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "mybucket")

# 클라이언트 생성 (연결 실패 시 에러가 나지 않도록 예외처리 할 수도 있음)
influx_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

# =========================================================
# [추가됨] 미들웨어: 모든 API 요청을 InfluxDB에 기록
# =========================================================
@app.middleware("http")
async def add_influxdb_middleware(request: Request, call_next):
    start_time = time.time()
    
    # 요청 처리
    response = await call_next(request)
    
    # 처리 시간 계산 (밀리초)
    process_time = (time.time() - start_time) * 1000
    
    # InfluxDB에 기록할 데이터 생성 (Point)
    # Measurement: "http_requests"
    # Tags: method(GET/POST), endpoint(/todos), status(200/404)
    # Fields: duration_ms(처리시간)
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

# =========================================================
# 기존 로직 (To-Do)
# =========================================================

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