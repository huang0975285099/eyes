#!/bin/bash

# MediaService - 本地构建并自动上传脚本（在有 Docker 的机器上使用）
# 使用方法: ./build.sh [--target 1|2] [-t tag] [--no-cache] [-h]
#
# 离线部署流程：
#   1. 本脚本在有网络的机器上构建 media-service 镜像并导出 tar
#   2. 连同 SRS、MySQL 镜像一起 scp 到目标服务器
#   3. 远程执行 deploy.sh 完成 load + 启动

set -e

# 无论从哪个目录调用，都以脚本所在目录作为构建上下文。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# 默认值
IMAGE_NAME="media-service"
AI_IMAGE_NAME="eyes-ai-service"
TAG="latest"
BUILD_NO_CACHE=false
DEPLOY_TARGET=""

# ================= 目标服务器配置 =================
# 两台服务器分别配置登录用户；生产 .env 只保存在各自服务器上。
PUBLIC_REMOTE_USER="test"
PUBLIC_REMOTE_IP="112.18.238.6"
PUBLIC_REMOTE_PORT="${PUBLIC_REMOTE_PORT:-2202}"
INTRANET_REMOTE_USER="administrator"
INTRANET_REMOTE_IP="10.0.20.219"
INTRANET_REMOTE_PORT="${INTRANET_REMOTE_PORT:-22}"
PUBLIC_REMOTE_DIR="/home/test/eyes/"
INTRANET_REMOTE_DIR="/home/administrator/eyes/"
PUBLIC_RECORDING_DIR="/home/test/recordings"
INTRANET_RECORDING_DIR="/home/administrator/eyesecordings"
# =================================================

# SRS 镜像（与 docker-compose.yml 中一致）
SRS_IMAGE="ossrs/srs:5"
SRS_TAR="srs-5.tar"
MYSQL_IMAGE="mysql:8.1.0"
MYSQL_TAR="mysql-8.1.0.tar"

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --target)
            if [ $# -lt 2 ]; then
                echo "错误: --target 后必须指定 1（公网）或 2（内网）"
                exit 1
            fi
            DEPLOY_TARGET="$2"
            shift 2
            ;;
        -t|--tag)
            if [ $# -lt 2 ] || [[ ! "$2" =~ ^[A-Za-z0-9_.-]+$ ]]; then
                echo "错误: 镜像标签只能包含字母、数字、点、下划线和连字符"
                exit 1
            fi
            TAG="$2"
            shift 2
            ;;
        --no-cache)
            BUILD_NO_CACHE=true
            shift
            ;;
        --cache)
            BUILD_NO_CACHE=false
            shift
            ;;
        -h|--help)
            echo "用法: $0 [--target 1|2] [-t tag] [--no-cache]"
            echo ""
            echo "参数:"
            echo "  --target      部署目标：1=公网，2=内网；不指定时交互选择"
            echo "  -t, --tag     指定镜像标签 (默认: latest)"
            echo "  --no-cache    不使用缓存，强制重新构建"
            echo "  --cache       使用缓存构建"
            echo "  -h, --help    显示帮助信息"
            echo ""
            echo "目标服务器:"
            echo "  1) 公网 ${PUBLIC_REMOTE_USER}@${PUBLIC_REMOTE_IP}:${PUBLIC_REMOTE_PORT}"
            echo "  2) 内网 ${INTRANET_REMOTE_USER}@${INTRANET_REMOTE_IP}:${INTRANET_REMOTE_PORT}"
            echo "  公网目录: ${PUBLIC_REMOTE_DIR}"
            echo "  内网目录: ${INTRANET_REMOTE_DIR}"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            exit 1
            ;;
    esac
done

if [ -z "${DEPLOY_TARGET}" ]; then
    echo "请选择部署目标："
    echo "  1) 公网 ${PUBLIC_REMOTE_USER}@${PUBLIC_REMOTE_IP}:${PUBLIC_REMOTE_PORT}"
    echo "  2) 内网 ${INTRANET_REMOTE_USER}@${INTRANET_REMOTE_IP}:${INTRANET_REMOTE_PORT}"
    if ! read -r -p "请输入 1 或 2: " DEPLOY_TARGET; then
        echo "错误: 无法读取部署目标；非交互执行请使用 --target 1 或 --target 2"
        exit 1
    fi
fi

# 兼容从Windows终端管道输入时可能携带的CR字符。
DEPLOY_TARGET="${DEPLOY_TARGET//$'\r'/}"

case "${DEPLOY_TARGET}" in
    1)
        TARGET_NAME="公网"
        REMOTE_USER="${PUBLIC_REMOTE_USER}"
        REMOTE_IP="${PUBLIC_REMOTE_IP}"
        REMOTE_PORT="${PUBLIC_REMOTE_PORT}"
        REMOTE_DIR="${PUBLIC_REMOTE_DIR}"
        RECORDING_DIR="${PUBLIC_RECORDING_DIR}"
        ;;
    2)
        TARGET_NAME="内网"
        REMOTE_USER="${INTRANET_REMOTE_USER}"
        REMOTE_IP="${INTRANET_REMOTE_IP}"
        REMOTE_PORT="${INTRANET_REMOTE_PORT}"
        REMOTE_DIR="${INTRANET_REMOTE_DIR}"
        RECORDING_DIR="${INTRANET_RECORDING_DIR}"
        ;;
    *)
        echo "错误: 部署目标只能是 1（公网）或 2（内网），当前值: ${DEPLOY_TARGET}"
        exit 1
        ;;
esac

FULL_IMAGE_NAME="${IMAGE_NAME}:${TAG}"
TAR_FILE="${IMAGE_NAME}-${TAG}.tar"
AI_FULL_IMAGE_NAME="${AI_IMAGE_NAME}:${TAG}"
AI_TAR_FILE="${AI_IMAGE_NAME}-${TAG}.tar"

echo "=========================================="
echo "  MediaService - 构建应用镜像"
echo "=========================================="
echo "  镜像名称: ${FULL_IMAGE_NAME}"
echo "  构建时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  部署目标: ${TARGET_NAME}"
echo "  目标服务器: ${REMOTE_USER}@${REMOTE_IP}:${REMOTE_PORT}"
echo "  远程目录: ${REMOTE_DIR}"
echo "=========================================="

# 检查 Docker 是否运行
if ! docker info > /dev/null 2>&1; then
    echo "✗ 错误: Docker 未运行，请先启动 Docker"
    exit 1
fi

# 检查 Dockerfile 是否存在
if [ ! -f "Dockerfile" ]; then
    echo "✗ 错误: 未找到 Dockerfile"
    exit 1
fi
if [ ! -f "../AIService/Dockerfile" ]; then
    echo "✗ 错误: 未找到 ../AIService/Dockerfile"
    exit 1
fi

echo ""
echo "[1/5] 清理旧镜像和构建缓存..."
docker rmi "${FULL_IMAGE_NAME}" 2>/dev/null || echo "  旧镜像不存在，跳过"
docker rmi "${AI_FULL_IMAGE_NAME}" 2>/dev/null || echo "  旧AI镜像不存在，跳过"

# 清理旧的 tar 文件
if [ -f "${TAR_FILE}" ]; then
    rm -f "${TAR_FILE}"
    echo "  已清理旧镜像文件: ${TAR_FILE}"
fi
if [ -f "${AI_TAR_FILE}" ]; then
    rm -f "${AI_TAR_FILE}"
    echo "  已清理旧镜像文件: ${AI_TAR_FILE}"
fi
if [ -f "${SRS_TAR}" ]; then
    rm -f "${SRS_TAR}"
fi
if [ -f "${MYSQL_TAR}" ]; then
    rm -f "${MYSQL_TAR}"
fi

echo ""
echo "[2/5] 构建新镜像..."
export DOCKER_BUILDKIT=1
BUILD_CMD="docker build -t ${FULL_IMAGE_NAME} ."

if [ "${BUILD_NO_CACHE}" = true ]; then
    echo "  使用 --no-cache 选项，强制重新构建"
    BUILD_CMD="${BUILD_CMD} --no-cache"
fi

${BUILD_CMD}

AI_BUILD_CMD="docker build -t ${AI_FULL_IMAGE_NAME} ../AIService"
if [ "${BUILD_NO_CACHE}" = true ]; then
    AI_BUILD_CMD="${AI_BUILD_CMD} --no-cache"
fi
${AI_BUILD_CMD}

echo ""
echo "[3/5] 验证镜像并导出..."
IMAGE_SIZE=$(docker images "${FULL_IMAGE_NAME}" --format "{{.Size}}")
IMAGE_ID=$(docker images "${FULL_IMAGE_NAME}" --format "{{.ID}}" | head -c 12)
echo "  镜像ID: ${IMAGE_ID}"
echo "  镜像大小: ${IMAGE_SIZE}"

# 导出应用镜像
docker save -o "${TAR_FILE}" "${FULL_IMAGE_NAME}"
echo "  ✓ 应用镜像已导出: ${TAR_FILE}"
docker save -o "${AI_TAR_FILE}" "${AI_FULL_IMAGE_NAME}"
echo "  ✓ AI镜像已导出: ${AI_TAR_FILE}"

# 拉取并导出依赖镜像
echo ""
echo "[4/5] 拉取并导出 SRS、MySQL 镜像..."
if [ -z "$(docker images -q ${SRS_IMAGE})" ]; then
    echo "  本地不存在 ${SRS_IMAGE}，开始拉取..."
    docker pull "${SRS_IMAGE}"
else
    echo "  本地已存在 ${SRS_IMAGE}，跳过拉取"
fi
docker save -o "${SRS_TAR}" "${SRS_IMAGE}"
echo "  ✓ SRS 镜像已导出: ${SRS_TAR} ($(du -h ${SRS_TAR} | cut -f1))"

if [ -z "$(docker images -q ${MYSQL_IMAGE})" ]; then
    echo "  本地不存在 ${MYSQL_IMAGE}，开始拉取..."
    docker pull "${MYSQL_IMAGE}"
else
    echo "  本地已存在 ${MYSQL_IMAGE}，跳过拉取"
fi
docker save -o "${MYSQL_TAR}" "${MYSQL_IMAGE}"
echo "  ✓ MySQL 镜像已导出: ${MYSQL_TAR} ($(du -h ${MYSQL_TAR} | cut -f1))"

echo ""
echo "[5/5] 上传到目标服务器..."
# 确保远程目录存在
ssh -p "${REMOTE_PORT}" "${REMOTE_USER}@${REMOTE_IP}" "mkdir -p '${REMOTE_DIR}'"

# 上传镜像 tar
echo "  上传 ${TAR_FILE}..."
scp -P "${REMOTE_PORT}" -C "${TAR_FILE}" "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}"
echo "  上传 ${AI_TAR_FILE}..."
scp -P "${REMOTE_PORT}" -C "${AI_TAR_FILE}" "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}"
echo "  上传 ${SRS_TAR}..."
scp -P "${REMOTE_PORT}" -C "${SRS_TAR}" "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}"
echo "  上传 ${MYSQL_TAR}..."
scp -P "${REMOTE_PORT}" -C "${MYSQL_TAR}" "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}"

# 上传部署脚本和配置文件
echo "  上传 deploy.sh, docker-compose.yml, srs.conf, .env.example..."
scp -P "${REMOTE_PORT}" -C deploy.sh docker-compose.yml srs.conf .env.example "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}"
ssh -p "${REMOTE_PORT}" "${REMOTE_USER}@${REMOTE_IP}" "chmod +x '${REMOTE_DIR}deploy.sh'"

# srs.conf 在 docker-compose 中挂载到 ./srs.conf，上传到远程根目录即可
echo "  ✓ 上传完成"

echo ""
echo "  在服务器上执行 deploy.sh..."
ssh -p "${REMOTE_PORT}" "${REMOTE_USER}@${REMOTE_IP}" "cd '${REMOTE_DIR}' && RECORDING_DIR='${RECORDING_DIR}' ./deploy.sh -t '${TAG}'"

echo ""
echo "=========================================="
echo "  ✓ 部署完成！"
echo "=========================================="
