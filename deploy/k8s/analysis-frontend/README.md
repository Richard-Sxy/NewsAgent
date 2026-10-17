# 前端 K8s 部署清单

发布一个纯静态、非 root、只读根文件系统的 nginx 容器，身份由 Ingress 上游的 SSO 网关注入。

## 清单构成

| 文件 | 作用 |
| --- | --- |
| `frontend-deployment.yaml` | 2 副本、滚动更新、只读根文件系统、`/healthz` 探针、反亲和打散 |
| `frontend-service.yaml` | ClusterIP，端口 8080 |
| `frontend-configmap.yaml` | 挂载为 `config.json` 的运行时配置，改配置只需重启 Pod |
| `frontend-ingress.yaml` | 两个 Ingress：`/` 静态资源、`/api` 鉴权并注入身份头 |
| `frontend-hpa.yaml` | CPU 70% 目标，2-6 副本 |
| `frontend-networkpolicy.yaml` | 仅允许 ingress-nginx 入站，出站只保留 DNS |

## 发布

```bash
kubectl kustomize deploy/k8s | kubectl apply -f -

# 或使用不可变 digest
cd deploy/k8s && kustomize edit set image \
  registry.internal/newsagent/frontend@sha256:<digest>
kubectl apply -k deploy/k8s
```

## 必须先补齐的三件事

清单里用到的以下依赖**本仓库当前不存在**，需要按实际环境提供：

1. **上游 SSO 网关**：两处 `auth-url` / `auth-signin` 目前是占位域名，
   且要求校验通过后返回 `X-Tenant-ID`、`X-User-ID`、`X-Data-Loop-Roles`、
   `X-Hot-News-Roles` 四个响应头。热点控制台与 Data Loop 复用同一个网关共享
   Token（`DATA_LOOP_GATEWAY_TOKEN`），但角色头各自独立、分别授权。
2. **API 的 K8s Service（`writing-agent-api`）**：仓库里只有 docker compose 的 `api` 服务，
   K8s 侧需要用同一镜像单独部署，并把 Service 端口命名为 `http` 才能与 Ingress 对齐。
3. **网关共享 Token 的 ConfigMap**：
   ```bash
   kubectl -n news-agent create configmap gateway-request-headers \
     --from-literal=Authorization="Bearer ${DATA_LOOP_GATEWAY_TOKEN}"
   ```
   不要把它写进 Ingress 注解 —— 注解会随清单进入 Git。

## 安全要点

- `more_clear_input_headers` 虽然出现在注解里，但它是**必须**的：
  没有它，客户端可以伪造 `X-Tenant-ID` 冒充任意租户。
- 后端 `app/api/dependencies.py::get_data_loop_principal` 与 `get_hot_news_principal`
  仍会独立校验共享 Bearer Token，所以网关注入错了也拿不到数据 —— 这是第二道防线，
  不能因此省掉第一道。
- 静态容器出站被 NetworkPolicy 限死为 DNS，即使 nginx 被攻破也没有横向移动路径。
