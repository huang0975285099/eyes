FROM golang:1.22-alpine AS builder
WORKDIR /app
# 设置 Go 代理（解决网络问题）
ENV GOPROXY=https://goproxy.cn,direct
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o recording-service .

FROM alpine:3.19
RUN apk add --no-cache ffmpeg tzdata ca-certificates wget
ENV TZ=Asia/Shanghai
WORKDIR /app
COPY --from=builder /app/recording-service .
VOLUME ["/var/recordings"]
EXPOSE 8089
CMD ["./recording-service"]
