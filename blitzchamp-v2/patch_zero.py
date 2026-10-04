from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text(encoding="utf-8")

s = s.replace(
    "from torch.nn.parallel import DistributedDataParallel as DDP\n",
    "from torch.nn.parallel import DistributedDataParallel as DDP\n"
    "try:\n"
    "    from torch.distributed.optim import ZeroRedundancyOptimizer\n"
    "except Exception:\n"
    "    ZeroRedundancyOptimizer = None\n",
)

old = '''def save_checkpoint(base_model, opt, scaler, step, out, val_history, args,
                    rank: int, world: int):
    barrier(world)
    if rank == 0:
        ckpt = {
            "model": base_model.state_dict(),
            "optimizer": opt.state_dict(),
'''
new = '''def save_checkpoint(base_model, opt, scaler, step, out, val_history, args,
                    rank: int, world: int):
    if getattr(args, "zero_redundancy", False) and world > 1:
        opt.consolidate_state_dict(to=0)
    barrier(world)
    if rank == 0:
        ckpt = {
            "model": base_model.state_dict(),
            "optimizer": opt.state_dict(),
'''
assert old in s
s = s.replace(old, new)

start = s.index("def make_optimizer(model, args, device):")
end = s.index("\n\ndef main():", start)
replacement = '''def make_optimizer(model, args, device, world):
    kwargs = dict(lr=args.lr, betas=(0.9, 0.95), eps=1e-8, weight_decay=args.weight_decay)
    if args.optimizer == "adamw8bit":
        import bitsandbytes as bnb
        return bnb.optim.AdamW8bit(model.parameters(), **kwargs), "adamw8bit"

    if args.zero_redundancy and world > 1:
        if ZeroRedundancyOptimizer is None:
            raise RuntimeError("ZeroRedundancyOptimizer unavailable")
        opt_kwargs = dict(kwargs)
        if args.optimizer in ("auto", "fused-adamw") and device.type == "cuda":
            opt_kwargs["fused"] = True
        try:
            return ZeroRedundancyOptimizer(
                model.parameters(), optimizer_class=torch.optim.AdamW, **opt_kwargs
            ), "zero-fused-adamw" if opt_kwargs.get("fused") else "zero-adamw"
        except (TypeError, RuntimeError) as exc:
            if args.optimizer == "fused-adamw":
                raise
            opt_kwargs.pop("fused", None)
            opt_kwargs["foreach"] = True
            print(f"ZeRO fused AdamW unavailable, falling back: {exc}", flush=True)
            return ZeroRedundancyOptimizer(
                model.parameters(), optimizer_class=torch.optim.AdamW, **opt_kwargs
            ), "zero-foreach-adamw"

    if args.optimizer in ("auto", "fused-adamw") and device.type == "cuda":
        try:
            return torch.optim.AdamW(model.parameters(), fused=True, **kwargs), "fused-adamw"
        except (TypeError, RuntimeError) as exc:
            if args.optimizer == "fused-adamw":
                raise
            print(f"fused AdamW unavailable, falling back: {exc}", flush=True)
    return torch.optim.AdamW(model.parameters(), foreach=True, **kwargs), "foreach-adamw"
'''
s = s[:start] + replacement + s[end:]

needle = '    ap.add_argument("--optimizer", choices=["auto", "fused-adamw", "adamw", "adamw8bit"], default="auto")\n'
assert needle in s
s = s.replace(
    needle,
    needle + '    ap.add_argument("--zero-redundancy", action="store_true")\n',
)
s = s.replace(
    "    opt, optimizer_name = make_optimizer(base_model, args, device)\n",
    "    opt, optimizer_name = make_optimizer(base_model, args, device, world)\n",
)
p.write_text(s, encoding="utf-8")
print("ZERO_PATCHED", p, len(s.encode("utf-8")))
