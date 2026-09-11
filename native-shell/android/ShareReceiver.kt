package com.repocollector.float

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Log

/**
 * 系统分享直达入口：用户在任何 App（抖音 / B 站 / 微信 / 浏览器）点「分享」，
 * 选择「收藏到收藏箱」即触发。本 Activity 取出分享的文本（链接或整段话），
 * 直接转交给 ResultActivity，由它用 WebView 打开收集器的 /collect 深链——
 * 收集逻辑全在服务端，客户端只负责把文本传过去并显示结果页。
 *
 * 这样把「复制 → 打开网页 → 粘贴」压成「分享 → 选本 App」一次点击。
 */
class ShareReceiver : Activity() {
    companion object {
        private const val TAG = "ShareReceiver"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val sharedText = extractSharedText(intent)
        val forward = Intent(this, ResultActivity::class.java).apply {
            action = Intent.ACTION_VIEW
            // 把整段文本交给 ResultActivity，由它打开 /collect?text=... 让服务端抽取链接
            putExtra("shared_text", sharedText ?: "")
        }
        startActivity(forward)
        finish()
    }

    /** 从 SEND / SEND_MULTIPLE 意图里取出文本。多条则合并成一段。 */
    private fun extractSharedText(intent: Intent): String? {
        return try {
            when (intent.action) {
                Intent.ACTION_SEND -> {
                    if ("text/plain" == intent.type) {
                        intent.getStringExtra(Intent.EXTRA_TEXT)
                    } else null
                }
                Intent.ACTION_SEND_MULTIPLE -> {
                    val list = intent.getCharSequenceArrayListExtra(Intent.EXTRA_TEXT)
                    list?.joinToString("\n") { it.toString() }
                }
                else -> null
            }
        } catch (e: Exception) {
            Log.e(TAG, "extract share text failed", e)
            null
        }
    }
}
