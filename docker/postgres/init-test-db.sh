#!/bin/sh
# ============================================================================
# PostgreSQL 首次初始化时额外创建一个测试库。
#
# 只在数据卷为空时执行一次（postgres 官方镜像的 /docker-entrypoint-initdb.d 机制）。
# 测试库与业务库分离，这样 `make test` 清表不会动到开发数据。
# ============================================================================
set -eu

if [ -z "${POSTGRES_TEST_DB:-}" ]; then
    echo "未设置 POSTGRES_TEST_DB，跳过测试库创建"
    exit 0
fi

echo "创建测试库: ${POSTGRES_TEST_DB}"

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" <<-EOSQL
    SELECT 'CREATE DATABASE "${POSTGRES_TEST_DB}" OWNER "${POSTGRES_USER}"'
    WHERE NOT EXISTS (
        SELECT FROM pg_database WHERE datname = '${POSTGRES_TEST_DB}'
    )\gexec
EOSQL

echo "测试库创建完成: ${POSTGRES_TEST_DB}"
