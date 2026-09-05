"""合成 PDF + 模拟模型的手动浏览器验收服务；仅绑定 loopback，所有数据在 tmp。"""
from pathlib import Path
import sys
import tempfile
import json
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]


def create_app():
    import fitz
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from app.api.v1 import reader, chat
    from core import history_store, reading_store, web_artifact_store
    from tools.ingest import attachments
    import core.llm as llm
    import agents.orchestrator as orchestrator
    import asyncio

    tmp = Path(tempfile.mkdtemp(prefix="paper-reader-e2e-"))
    history_store.HISTORY_DIR = tmp / "history"
    reading_store.READING_DB = tmp / "reading.db"
    web_artifact_store.WEB_ARTIFACT_DB = tmp / "owners.db"
    attachments.UPLOAD_DIR = tmp / "uploads"
    reader.current_user = lambda *_: {"id": "local"}
    chat.current_user = lambda *_: {"id": "local"}
    chat._UPLOAD_DIR = attachments.UPLOAD_DIR
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:3107"], allow_methods=["*"], allow_headers=["*"])
    app.include_router(reader.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")

    @app.get("/api/v1/auth/config")
    def config():
        return {"auth_required": False, "registration_open": False, "guest_access": True, "email_requirement": "none"}

    async def translate(*_args, **_kwargs):
        await asyncio.sleep(.15)
        return SimpleNamespace(content="在固定条件下，我们的模型可能提高准确率。\n\n**术语说明**：fixed conditions 指实验设置中固定的数据分布与评估条件。公式 $L = \\sum_i (y_i - \\hat y_i)^2$ 保持变量不变。")
    llm.get_llm = lambda *_: object()
    llm.ainvoke_utility = translate
    orchestrator._index_attachments = lambda *_: None
    async def turn(prompt, session, **kwargs):
        yield {"type": "thinking", "content": "正在检查当前页提供的条件与作者表述。"}
        for text in ["这句话保留了两个重要限定：**可能**提高，以及**固定条件下**。", "\n\n需要核对实验是否覆盖变化的数据分布。当前选文不能支持所有场景都更好的结论。", "\n\n[第 1 页](#page=1) · 这是合成论文的模拟助读结果。"]:
            await asyncio.sleep(.1)
            yield {"type": "answer", "content": text}
        yield {"type": "done", "answer": "固定条件下可能改善准确率；变化的数据分布需要另行验证。 [第 1 页](#page=1)"}
    orchestrator.chat_turn = turn

    with fitz.open() as doc:
        p = doc.new_page(width=595, height=842)
        p.insert_text((54, 48), "RESEARCH NOTES / SYNTHETIC DEMONSTRATION", fontsize=8, color=(.37,.48,.41))
        p.insert_text((54, 97), "Learning Under Fixed Conditions", fontsize=24, fontname="Times-Bold")
        p.insert_text((54, 121), "A controlled study of representations and their limits", fontsize=13, fontname="Times-Roman")
        p.insert_text((54, 150), "Paper Agent Test Lab  |  Browser verification fixture", fontsize=10, color=(.4,.4,.4))
        p.draw_line((54,169),(541,169),color=(.7,.75,.7),width=.5)
        p.insert_text((54, 202), "Abstract", fontsize=15,fontname="Times-Bold")
        p.insert_textbox(fitz.Rect(54,220,541,300), "Our model may improve accuracy under fixed conditions. We examine representations through a controlled evaluation. The results describe a bounded experimental setting, rather than a universal guarantee. This document is synthetic and contains no real research claims.",fontsize=11,fontname="Times-Roman",lineheight=1.5)
        p.insert_text((54,327),"1  Introduction",fontsize=14,fontname="Times-Bold")
        p.insert_textbox(fitz.Rect(54,349,279,585), "Reliable reading requires distinguishing a claim from the evidence supporting it. A model can produce an explanation, but the reader must still inspect the original conditions.\n\nIn this illustrative experiment, the training distribution remains fixed. We compare two representations using the same evaluation protocol.\n\nThe phrase 'may improve' does not imply improvement in every setting. The scope of the evidence matters.",fontsize=11,fontname="Times-Roman",lineheight=1.45)
        p.insert_textbox(fitz.Rect(308,349,541,585), "Our model may improve accuracy under fixed conditions.\n\nThe evaluation reports the average score across repeated trials. A higher average alone does not establish robustness to distribution shift.\n\nWe retain the original page layout, tables, and figures so that readers can inspect the source while discussing it. See Table 1 on the next page.",fontsize=11,fontname="Times-Roman",lineheight=1.45)
        p.draw_rect(fitz.Rect(54,610,541,727),fill=(.94,.96,.93),color=None)
        for i,h in enumerate([25,45,65,53,78]):
            x=90+i*82;p.draw_rect(fitz.Rect(x,704-h,x+35,704),fill=(.28,.49,.4),color=None)
        p.insert_text((54,754),"Figure 1. Synthetic scores for interface testing only.",fontsize=9,fontname="Times-Italic")
        p.insert_text((290,807),"1",fontsize=9)
        p=doc.new_page(width=595,height=842)
        p.insert_text((54,75),"2  Method and Results",fontsize=23,fontname="Times-Bold")
        p.insert_text((54,115),"The evaluation uses a fixed loss and identical data splits.",fontsize=11,fontname="Times-Roman")
        p.insert_text((145,167),"L = sum_i (y_i - prediction_i)^2",fontsize=15,fontname="Times-Italic")
        p.insert_text((54,240),"Table 1. Synthetic comparison, not research evidence.",fontsize=11,fontname="Times-Roman")
        for i,row in enumerate(["Method                 Score             Conditions", "Baseline               42.5              Fixed", "Representation B       47.2              Fixed", "Distribution shift     Not measured      Unknown"]):
            y=280+i*35;p.insert_text((64,y),row,fontsize=11,fontname="Courier");p.draw_line((54,y+12),(541,y+12),color=(.75,.8,.75),width=.5)
        p.insert_text((54,500),"3  Limitations",fontsize=18,fontname="Times-Bold")
        p.insert_textbox(fitz.Rect(54,527,541,700),"This evaluation does not measure changing data distributions. These illustrative numbers cannot establish general effectiveness. A reader should record this limitation before comparing the result with another paper.",fontsize=12,fontname="Times-Roman",lineheight=1.5)
        p.insert_text((290,807),"2",fontsize=9)
        p=doc.new_page(width=595,height=842);p.insert_text((54,75),"Rotated page fixture",fontsize=20);p.insert_text((54,115),"Rotation must preserve selection and highlight geometry.",fontsize=12);p.set_rotation(90)
        doc.new_page(width=595,height=842)
        doc.set_toc([[1,"Abstract",1],[1,"1 Introduction",1],[1,"2 Method and Results",2],[1,"3 Limitations",2],[1,"Rotated fixture",3],[1,"Scanned-page fallback",4]])
        rec=attachments.save_attachment(doc.tobytes(),"Learning Under Fixed Conditions.pdf",owner_id="local")
    Path("/tmp/paper-reader-e2e.json").write_text(json.dumps({"attachment":rec,"data_dir":str(tmp)}))
    return app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8107, log_level="warning")
