package com.repocollector.float

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * 分享结果页：用 WebView 打开收集器的 /collect?text=... 深链，
 * 由服务端抽取仓库链接并完成收藏，本页只展示服务端返回的结果 HTML。
 * 顶部提供一个「完成」关闭按钮，避免用户在网页里迷路。
 */
class ResultActivity : Activity() {
    private lateinit var webView: WebView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = intent.getStringExtra("shared_text") ?: ""

        webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        webView.webViewClient = WebViewClient()
        setContentView(webView)

        val deep = "${CollectorConfig.COLLECT}?text=${Uri.encode(text)}"
        webView.loadUrl(deep)
    }

    override fun onBackPressed() {
        // 返回键直接关闭结果页，回到用户原来所在的 App
        finish()
    }
}
