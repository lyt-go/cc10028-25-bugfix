"""人工复核服务：低置信抽取结果的人工复核任务、复核结论登记与退回原因追溯。

从抽取质量报告筛选需要人工复核的低置信度条目，创建复核任务并记录结论。
空批次读取空批质检回执，登记"无需抽检"或"仍待上游"业务状态并保留批次凭据。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AnnotReviewService(BaseModuleService):
    MODULE = "annot_review"
    ENTITY = "复核任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["复核编号", "分块编号", "复核对象", "原判定", "复核结论", "复核人", "复核时间", "退回原因"]
    REQUIRED_FIELDS = ["复核编号", "分块编号", "复核对象"]
    STATUS_ORDER = ["草稿", "待复核", "复核中", "已结案"]
    ACTION_RULES = {"分派复核": "待复核", "提交结论": "已结案", "标记退回": "已结案"}
    NEGATIVE_ACTIONS = []

    def create_review_tasks(self, batch_key: str, threshold: float = 0.80) -> dict[str, Any]:
        """从低置信度抽取结果中创建人工复核任务。

        没有实际记录可复核时不虚造任务：读取空批质检回执，同步登记
        "无需抽检"（两种来源均已确认完成）或"仍待上游"（来源未产出、
        抽取中断或部分失败）的业务状态回执，并保留批次凭据。
        """
        entity_files = self._batch_source_files("entities", batch_key, "_entities")
        relation_files = self._batch_source_files("relations", batch_key, "_relations")

        output_path = self._data_dir("reviews") / f"{batch_key}_reviews.jsonl"

        # 核对来源完成状态：任一来源未产出则复核仍需等待上游
        missing_sources = []
        if not entity_files:
            missing_sources.append("实体抽取产物")
        if not relation_files:
            missing_sources.append("关系抽取产物")

        # 读取空批质检回执，作为批次凭据与判定依据
        quality_cp = self._read_checkpoint(f"quality_{batch_key}") or {}
        batch_credential = f"QC-{batch_key}"

        if missing_sources:
            reason = (quality_cp.get("pending_reason")
                      or "、".join(missing_sources) + "尚未产出，等待上游完成后重新质检")
            return self._register_batch_receipt(
                output_path, batch_key, batch_credential, threshold,
                business_status="仍待上游",
                adjudication=quality_cp.get("pending_reason", ""),
                reason=reason,
            )

        tasks = []
# 数据操作：校验复核任务数据创建低置信度条目的复核任务
        task_counter = 0

        # 筛选低置信度实体
        for ef in entity_files:
            entities = self._read_jsonl(ef)
            for e in entities:
                if e.get("confidence", 1.0) < threshold:
                    task_counter += 1
                    tasks.append({
                        "复核编号": f"REV-{task_counter:06d}",
                        "分块编号": e.get("chunk_id", ""),
                        "复核对象": f"实体: {e.get('entity_text')} [{e.get('entity_type')}]",
                        "原判定": f"置信度 {e.get('confidence', 0):.2f}",
                        "复核结论": "",
                        "复核人": "",
                        "复核时间": "",
                        "退回原因": "",
                        "status": "待复核",
                    })

        # 筛选低置信度关系
        for rf in relation_files:
            relations = self._read_jsonl(rf)
            for r in relations:
                if r.get("confidence", 1.0) < threshold:
                    task_counter += 1
                    tasks.append({
                        "复核编号": f"REV-{task_counter:06d}",
                        "分块编号": "",
                        "复核对象": f"关系: {r.get('head_entity')}→{r.get('relation_type')}→{r.get('tail_entity')}",
# 数据产物：复核任务清单写入reviews目录持久化文件
                        "原判定": f"置信度 {r.get('confidence', 0):.2f}",
                        "复核结论": "",
                        "复核人": "",
                        "复核时间": "",
                        "退回原因": "",
                        "status": "待复核",
                    })

        # 没有实际记录可复核：不虚造复核任务，登记"无需抽检"业务状态回执
        if not tasks:
            reason = (quality_cp.get("conclusion")
                      or "批次来源均已确认完成，无低置信度记录需人工复核")
            return self._register_batch_receipt(
                output_path, batch_key, batch_credential, threshold,
                business_status="无需抽检",
                adjudication=quality_cp.get("conclusion", ""),
                reason=reason,
            )

        # 写入复核任务清单（覆盖式刷新，重新提交时保持与当前来源一致）
        written = self._rewrite_jsonl(output_path, tasks)

        self._write_checkpoint(f"review_{batch_key}", {
            "batch_key": batch_key,
            "threshold": threshold,
            "tasks_created": len(tasks),
            "business_status": "待复核",
            "batch_credential": batch_credential,
            "entities_reviewed": sum(1 for t in tasks if "实体" in t["复核对象"]),
            "relations_reviewed": sum(1 for t in tasks if "关系" in t["复核对象"]),
        })

        return {
            "batch_key": batch_key,
            "threshold": threshold,
            "tasks_created": len(tasks),
            "business_status": "待复核",
            "records_written": written,
            "output": str(output_path),
        }

    def _register_batch_receipt(
        self,
        output_path,
        batch_key: str,
        batch_credential: str,
        threshold: float,
        *,
        business_status: str,
        adjudication: str,
        reason: str,
    ) -> dict[str, Any]:
        """登记批次级复核回执：无实际记录可复核时同步业务状态并保留批次凭据。"""
        receipt = {
            "复核编号": f"REV-{batch_key}",
            "分块编号": "",
            "复核对象": f"批次: {batch_key}",
            "原判定": adjudication or reason,
            "复核结论": business_status,
            "复核人": "系统登记",
            "复核时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "退回原因": reason if business_status == "仍待上游" else "",
            "status": business_status,
            "批次凭据": batch_credential,
        }
        # 覆盖式写入：同一批次重新提交时刷新为当前业务状态
        written = self._rewrite_jsonl(output_path, [receipt])

        self._write_checkpoint(f"review_{batch_key}", {
            "batch_key": batch_key,
            "threshold": threshold,
            "tasks_created": 0,
            "business_status": business_status,
            "reason": reason,
            "batch_credential": batch_credential,
        })

        return {
            "batch_key": batch_key,
            "threshold": threshold,
            "tasks_created": 0,
            "business_status": business_status,
            "reason": reason,
            "batch_credential": batch_credential,
            "records_written": written,
            "output": str(output_path),
        }

    def submit_review_conclusion(self, review_id: str, conclusion: str, reviewer: str) -> dict[str, Any]:
        """提交复核结论并更新任务状态。"""
        # 读取复核文件
        review_files = sorted(self._data_dir("reviews").glob("*.jsonl"))
        for rf in review_files:
            tasks = self._read_jsonl(rf)
            for task in tasks:
                if task.get("复核编号") == review_id:
                    task["复核结论"] = conclusion
                    task["复核人"] = reviewer
                    task["复核时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    task["status"] = "已结案"
                    # 回写
                    self._write_jsonl(rf, [])
                    # 重写整个文件
                    with open(rf, "w", encoding="utf-8") as f:
                        import json
                        for t in tasks:
                            f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")
                    return {"review_id": review_id, "conclusion": conclusion, "reviewer": reviewer, "status": "已结案"}
        return {"error": f"复核任务 {review_id} 不存在"}