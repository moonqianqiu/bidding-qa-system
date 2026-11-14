"use client";

// v2.3 双通道图片附件（B-flow）：选图只建档（本地存 File 不上传），发送时才
// 双压缩 + POST /api/vision 换 image_ref——被丢弃的附件不再烧视觉调用。
// 失败语义与 v2.2 一致：chip 留错态，发送继续但不带图（图片上下文尽力而为）。

import { useCallback, useRef, useState } from "react";
import type { Attachment } from "@/lib/types";
import { compressImage } from "@/lib/image";
import { postVision } from "@/lib/api";

export type DocMode = "auto" | "ocr"; // 开关：关=Qwen 自动路由，开=强制按文档解析

export function useImageAttachment() {
  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [preparing, setPreparing] = useState(false);
  const fileRef = useRef<File | null>(null);

  const pickFile = useCallback(
    (file: File) => {
      if (preparing) return;
      fileRef.current = file;
      setAttachment({ name: file.name }); // chip 即时出现，解析挪到发送时
    },
    [preparing],
  );

  const remove = useCallback(() => {
    fileRef.current = null;
    setAttachment(null);
  }, []);

  // 发送前解析：preview(1024/0.85) 给 Qwen 路由，original(2000/0.92) 供 OCR 通道
  //（auto 路由可能判到文档、ocr 手动必用，两种模式都带上）
  const prepareForSend = useCallback(
    async (mode: DocMode): Promise<string | undefined> => {
      const file = fileRef.current;
      if (!file || preparing) return undefined;
      setPreparing(true);
      try {
        const preview = await compressImage(file); // 长边≤1024 / JPEG 0.85 / 白底（A3）
        const original = await compressImage(file, 2000, 0.92);
        const r = await postVision(preview, file.name, { original, mode });
        fileRef.current = null;
        setAttachment(null); // 成功即消耗（描述 TTL 10min，不跨轮复用）
        return r.image_ref;
      } catch {
        setAttachment((a) => (a ? { ...a, error: true } : a)); // 留错态 chip，不阻塞发送
        return undefined;
      } finally {
        setPreparing(false);
      }
    },
    [preparing],
  );

  return { attachment, preparing, pickFile, remove, prepareForSend };
}
