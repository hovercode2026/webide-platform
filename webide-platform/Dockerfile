FROM docker.m.daocloud.io/library/python:3.11-slim
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir --progress-bar off -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
COPY app/ /app/
EXPOSE 8080
CMD ["python", "main.py"]
