import { redirect } from "next/navigation";

export default async function NodePermalink({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  redirect(`/knowledge?node=${id}`);
}
