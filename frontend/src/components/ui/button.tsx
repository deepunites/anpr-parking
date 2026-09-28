import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

/* Одна зелёная кнопка на экране — та, что берёт деньги (variant="money"); синяя — основное действие;
   крупные цели нажатия (≥ 44px) для сенсорных экранов в будке. */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-[var(--radius-ctl)] font-bold transition-colors focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50 whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "bg-accent text-on-accent hover:brightness-110",
        money: "bg-ok text-white hover:brightness-110",
        secondary: "bg-surface-3 text-ink hover:bg-line",
        danger: "bg-bad text-white hover:brightness-110",
        success: "bg-ok text-white hover:brightness-110",
        warn: "bg-warn text-[#1b1400] hover:brightness-105",
        ghost: "text-ink-2 hover:bg-surface-3",
        outline: "border border-line bg-surface text-ink hover:bg-surface-2",
      },
      size: { sm: "h-9 px-3 text-sm", md: "h-11 px-4 text-[15px]", lg: "h-13 px-6 text-base", xl: "h-14 px-6 text-lg", icon: "h-11 w-11" },
    },
    defaultVariants: { variant: "default", size: "md" },
  },
);

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, type = "button", ...props }, ref) => (
  <button ref={ref} type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />
));
Button.displayName = "Button";
