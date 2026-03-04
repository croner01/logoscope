# logoscope Helm Chart

## 打包

```bash
docker run --rm -v $(pwd):/work -w /work alpine/helm:3.15.4 lint charts/logoscope
docker run --rm -v $(pwd):/work -w /work alpine/helm:3.15.4 package charts/logoscope -d dist/helm
```

## 安装

```bash
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace
```

## 关闭组件示例

```bash
helm upgrade --install logoscope charts/logoscope -n islap \
  --set components.fluentBit.enabled=false \
  --set components.otelCollector.enabled=false
```
