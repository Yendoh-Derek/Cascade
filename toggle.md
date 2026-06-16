"use client"

import { useState } from "react"
import { cn } from "@/lib/utils"

interface PremiumToggleProps {
defaultChecked?: boolean
onChange?: (checked: boolean) => void
label?: string
}

export function PremiumToggle({ defaultChecked = false, onChange, label }: PremiumToggleProps) {
const [isChecked, setIsChecked] = useState(defaultChecked)
const [isPressed, setIsPressed] = useState(false)

const handleToggle = () => {
const newValue = !isChecked
setIsChecked(newValue)
onChange?.(newValue)
}

return (

<div className="flex items-center gap-3">
{label && (
<span
className={cn(
"text-sm font-medium transition-colors duration-300",
isChecked ? "text-foreground" : "text-muted-foreground",
)} >
{label}
</span>
)}
<button
role="switch"
aria-checked={isChecked}
onClick={handleToggle}
onMouseDown={() => setIsPressed(true)}
onMouseUp={() => setIsPressed(false)}
onMouseLeave={() => setIsPressed(false)}
className={cn(
"group relative h-8 w-14 rounded-full p-1 transition-all duration-500 ease-out",
"focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
isChecked ? "bg-foreground" : "bg-muted-foreground/20",
)} >
{/_ Glow effect _/}
<div
className={cn(
"absolute inset-0 rounded-full transition-opacity duration-500",
isChecked ? "opacity-100 shadow-[0_0_20px_rgba(0,0,0,0.15)]" : "opacity-0",
)}
/>

        {/* Track inner gradient */}
        <div
          className={cn(
            "absolute inset-[2px] rounded-full transition-all duration-500",
            isChecked ? "bg-gradient-to-b from-foreground to-foreground/90" : "bg-transparent",
          )}
        />

        {/* Thumb */}
        <div
          className={cn(
            "relative h-6 w-6 rounded-full shadow-lg transition-all duration-500 ease-[cubic-bezier(0.68,-0.55,0.265,1.55)]",
            "bg-background",
            isChecked ? "translate-x-6" : "translate-x-0",
            isPressed && "scale-90 duration-150",
          )}
        >
          {/* Thumb inner shine */}
          <div className="absolute inset-[2px] rounded-full bg-gradient-to-b from-background via-background to-muted/30" />

          {/* Thumb highlight */}
          <div className="absolute inset-0 rounded-full bg-gradient-to-br from-background/80 via-transparent to-transparent" />

          {/* Status indicator dot */}
          <div
            className={cn(
              "absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full transition-all duration-500",
              isChecked
                ? "h-2 w-2 bg-foreground opacity-100 scale-100"
                : "h-1.5 w-1.5 bg-muted-foreground/40 opacity-100 scale-100",
            )}
          />

          {/* Ripple effect on toggle */}
          <div
            className={cn(
              "absolute inset-0 rounded-full transition-all duration-700",
              isChecked ? "animate-ping bg-foreground/20 scale-150 opacity-0" : "scale-100 opacity-0",
            )}
            key={isChecked ? "on" : "off"}
          />
        </div>
      </button>
    </div>

)
}
