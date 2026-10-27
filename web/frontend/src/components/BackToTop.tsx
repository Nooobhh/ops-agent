import { useEffect, useState } from "react"
import { ArrowUp } from "lucide-react"
import { Button } from "@/components/ui/button"


export function BackToTop() {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const update = () => setVisible(window.scrollY > window.innerHeight)
    update()
    window.addEventListener("scroll", update, {passive: true})
    window.addEventListener("resize", update)
    return () => {
      window.removeEventListener("scroll", update)
      window.removeEventListener("resize", update)
    }
  }, [])

  return visible ? (
    <Button
      aria-label="回到顶部"
      className="fixed bottom-6 right-6 z-40 size-11 rounded-full shadow-lg"
      onClick={() => window.scrollTo({top: 0, behavior: "smooth"})}
      size="icon"
      title="回到顶部"
    >
      <ArrowUp />
    </Button>
  ) : null
}
