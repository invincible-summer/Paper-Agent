"use client";
import { ReadingWorkbench } from "@/components/reader/ReadingWorkbench";

export default function ReadingPage({ params }: { params: { sessionId: string; attachmentId: string } }) {
  return <ReadingWorkbench key={`${params.sessionId}:${params.attachmentId}`} sessionId={params.sessionId} attachmentId={params.attachmentId} />;
}
