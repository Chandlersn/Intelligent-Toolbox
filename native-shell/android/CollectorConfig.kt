package com.repocollector.float

/**
 * 收集器地址：唯一来源。
 *
 * 之前 OverlayService 与 ResultActivity 各写一份 COLLECTOR_BASE，改地址要改两处，
 * 漏一处就会出现「球弹得出来、但收藏不进去」这类难查的故障。现在都从这里读。
 *
 * 用哪个地址：
 * - 模拟器：http://10.0.2.2:8732（10.0.2.2 回环到宿主机）
 * - 真机：电脑的局域网 IP，手机与电脑同一 Wi-Fi，如 http://192.168.1.20:8732
 * - 发布：公网 https 地址，并去掉 AndroidManifest 里的 usesCleartextTraffic
 *
 * 后端侧要配合：
 * - 真机访问需要后端不再只绑回环：REPO_HOST=0.0.0.0 python run.py
 * - 若开了 REPO_TOKEN，还要把手机所在主机加进 REPO_ALLOW_ORIGINS
 */
object CollectorConfig {
    const val BASE = "http://10.0.2.2:8732"
    const val COLLECT = "$BASE/collect"
    const val HOST_HTML = "$BASE/host.html"
}
