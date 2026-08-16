import { redirect } from "next/navigation";

// The structured workflow page is gone — chat is the only surface.
export default function Home() {
  redirect("/chat");
}
