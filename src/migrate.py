"""多轴数据维护 CLI：重建 / 自检。

轴表是派生物（卡片才是真源），所以这个脚本的价值是「随时把派生数据拉回一致」。

    python src/migrate.py            # 从全部卡片重建 repo_axis / tags（不调 LLM，快）
    python src/migrate.py --doctor   # 只做自检：报告卡片与轴表不一致的条目

历史：本文件原名 migrate.py，做的是「把旧 card JSON 灌进轴表」。
改造后轴表可全量重建，一次性迁移脚本不再需要，保留文件名是为了不改旧文档与肌肉记忆。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402


def main():
    server.init_db()
    if "--doctor" in sys.argv:
        rep = server.doctor()
        print("库：%s（schema v%d，journal=%s）" % (
            rep["db"]["path"], rep["db"]["schema_version"], rep["db"]["journal_mode"]))
        print("卡片数：%d" % rep["cards"])
        print("LLM：%s" % ("可用" if rep["llm"]["available"] else "不可用（走规则）"))
        if rep["ok"]:
            print("一致性：OK，卡片与轴表完全一致。")
        else:
            print("一致性：发现 %d 条轴表与卡片不一致、%d 条缺轴。"
                  % (len(rep["axis_mismatch"]), len(rep["axis_missing"])))
            for m in rep["axis_mismatch"][:10]:
                print("  - id=%s 卡片=%s 轴表=%s  %s"
                      % (m["id"], m["card"], m["axis"], m["url"]))
            print("  修复：python src/migrate.py")
        return 0
    n = server.reindex_axes()
    print("重建完成：%d 条收藏的多轴数据已从卡片重新派生。" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
