"""Synthetic files/text only. No P/C, sockets, reference imports or database."""
import copy
import hashlib
import json
import os

import pytest

from alrust_conformance import observer_binding_reader as br
from alrust_conformance import stage_journal as sj


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, record):
    data = sj._dumps(record)
    path.write_bytes(data)
    path.chmod(0o600)
    return digest(data)


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    ref = root / "reference"
    ref.mkdir(mode=0o700)
    ri = root.stat()
    core = dict(schema=sj.SCHEMA_CORE, repository="steliosx/alrust", run_id="123",
                run_attempt="1", job="profile", profile="scaffold",
                event_head_sha="a"*40, checkout_sha="b"*40, reference_sha="c"*40,
                lock_sha256={"build.lock":"d"*64}, manifest_sha256="e"*64,
                runner_image={"os":"ubuntu24", "version":"20261001.1"},
                boot_id=sj.boot_id(), uid=os.getuid(),
                external_config={"path":sj.EXTERNAL_CONFIG,"state":"absent"},
                job_root={"path":str(root),"dev":ri.st_dev,"ino":ri.st_ino},
                captured_utc_ns=100, captured_monotonic_ns=100)
    observer = {"socket":str(ref/"observer.sock"),
                "manifest":str(ref/"observer-install.json"),"manifest_sha256":"f"*64}
    launch = dict(schema="alrust-reference-launch/1",
                  supervisor={"pid":10000,"pgid":10000,"start_ticks":123,
                              "uid":os.getuid(),"subreaper":1},
                  child={"pid":10001,"start_ticks":124}, boot_id=core["boot_id"],
                  venv={"path":str(ref/"venv"),"dev":ri.st_dev,"ino":12345},
                  terminal_receipt=str(ref/"terminal.json"),
                  observer_socket=observer["socket"],observer=observer,
                  launched_utc_ns=200,monotonic_ns=200)
    args = dict(job_root=str(root),root_identity=(ri.st_dev,ri.st_ino),
                expected_core={k:copy.deepcopy(core[k]) for k in br.CORE_EXPECTATIONS},
                core_sha256=dump(root/sj.CORE_NAME,core),
                launch_sha256=dump(ref/"launch.json",launch),
                expected_version="9.1.1",expected_port=18080,
                expected_venv_path=launch["venv"]["path"],observer_manifest_sha256="f"*64)
    refresh_readiness(args,launch)
    return root,core,launch,args


def refresh_readiness(args,launch):
    line=("reference alerta %s up on :%d (supervisor %d, child %d, "
          "receipt %s/reference/launch.json sha256 %s)\n" % (
              args["expected_version"],args["expected_port"],launch["supervisor"]["pid"],
              launch["child"]["pid"],args["job_root"],args["launch_sha256"])).encode()
    args.update(readiness_line=line,readiness_sha256=digest(line))


def test_synthetic_bound_inputs_keep_event_and_checkout_distinct(fixture):
    root,core,launch,args=fixture
    before={p.name:p.read_bytes() for p in (root/sj.CORE_NAME,root/"reference/launch.json")}
    result=br.read_binding_inputs(**args)
    assert result.supervisor==(10000,123) and result.child==(10001,124)
    assert result.core_bytes==(root/sj.CORE_NAME).read_bytes()
    assert json.loads(result.core_bytes)["event_head_sha"]!=json.loads(result.core_bytes)["checkout_sha"]
    assert result.scope=="CORE_LAUNCH_READINESS_BYTES_ONLY_NOT_STAGE_OR_PROCESS_QUALIFICATION"
    assert before=={p.name:p.read_bytes() for p in (root/sj.CORE_NAME,root/"reference/launch.json")}
    assert not (root/"reference/venv").exists()  # Identity declaration is NOT inspection.


@pytest.mark.parametrize("field,value",[
    ("repository","other/alrust"),("run_id","124"),("run_attempt","2"),
    ("job","other"),("profile","other"),("event_head_sha","1"*40),
    ("checkout_sha","1"*40),("reference_sha","1"*40),
    ("lock_sha256",{"build.lock":"1"*64}),("manifest_sha256","1"*64),
    ("runner_image",{"os":"other","version":"1"}),
    ("uid",True),("job_root",{"path":"/tmp/not-this-job","dev":1,"ino":1})])
def test_independent_core_expectations_refuse_mismatch(fixture,field,value):
    *_,args=fixture
    args["expected_core"][field]=value
    with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)


@pytest.mark.parametrize("path,value",[
    (("schema",),"other"),(("supervisor","pid"),True),
    (("supervisor","pgid"),10002),(("supervisor","start_ticks"),False),
    (("supervisor","subreaper"),True),(("supervisor","subreaper"),0),
    (("supervisor","uid"),True),
    (("child","pid"),10000),(("child","start_ticks"),None),
    (("child","start_ticks"),False),(("venv","dev"),True),
    (("venv","path"),"/tmp/elsewhere"),(("observer",),None),
    (("observer","manifest_sha256"),"1"*64),(("observer_socket",),"/tmp/elsewhere"),
    (("terminal_receipt",),"/tmp/elsewhere"),(("monotonic_ns",),99),
    (("monotonic_ns",),True),(("launched_utc_ns",),False),
    (("boot_id",),"00000000-0000-0000-0000-000000000000")])
def test_rehashed_forged_launch_does_not_satisfy_schema_and_bindings(fixture,path,value):
    root,_,launch,args=fixture
    target=launch
    for part in path[:-1]:target=target[part]
    target[path[-1]]=value
    args["launch_sha256"]=dump(root/"reference/launch.json",launch)
    refresh_readiness(args,launch)
    with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)


@pytest.mark.parametrize("which",["core","launch","readiness"])
def test_original_digest_required(fixture,which):
    *_,args=fixture
    args[which+"_sha256"]="1"*64
    with pytest.raises(br.BindingReadError,match=which+"_digest_mismatch"):
        br.read_binding_inputs(**args)


@pytest.mark.parametrize("target",["core","launch","supervisor","child","venv","observer"])
def test_rehashed_extra_fields_never_extend_the_schema(fixture,target):
    root,core,launch,args=fixture
    if target=="core":
        core["extra"]=None
        args["core_sha256"]=dump(root/sj.CORE_NAME,core)
    else:
        (launch if target=="launch" else launch[target])["extra"]=None
        args["launch_sha256"]=dump(root/"reference/launch.json",launch)
        refresh_readiness(args,launch)
    with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)


def test_nonfinite_independent_input_has_fixed_diagnostic(fixture):
    *_,args=fixture
    args["expected_core"]["uid"]=float('nan')
    with pytest.raises(br.BindingReadError,match="binding_read_failed"):
        br.read_binding_inputs(**args)


@pytest.mark.parametrize("mutation",[
    lambda b:b.replace(b":18080",b":18081"),
    lambda b:b.replace(b"supervisor 10000",b"supervisor 99999"),
    lambda b:b.replace(b"child 10001",b"child 99999"),
    lambda b:b+b"extra stdout\n",lambda b:b.rstrip(),lambda b:b.replace(b"9.1.1",b"9.1.2")])
def test_rehashed_readiness_must_bind_exact_source_ids(fixture,mutation):
    *_,args=fixture
    args["readiness_line"]=mutation(args["readiness_line"])
    args["readiness_sha256"]=digest(args["readiness_line"])
    with pytest.raises(br.BindingReadError,match="readiness_binding_mismatch"):
        br.read_binding_inputs(**args)


@pytest.mark.parametrize("bad",[b'{"schema":"x","schema":"y"}',b'{"x":NaN}',
                               b'{"x":1e999}',b'\xff',b'{',b'['*2000])
def test_rehashed_malformed_json_is_refused(fixture,bad):
    root,_,launch,args=fixture
    (root/"reference/launch.json").write_bytes(bad)
    args["launch_sha256"]=digest(bad);refresh_readiness(args,launch)
    with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)


@pytest.mark.parametrize("target",["core-identity.json","reference/launch.json"])
@pytest.mark.parametrize("kind",["symlink","fifo","oversize","public","missing"])
def test_unsafe_private_files_refused_without_blocking(fixture,tmp_path,target,kind):
    root,*_,args=fixture;p=root/target
    data=p.read_bytes();p.unlink()
    if kind=="symlink":
        other=tmp_path/"other";other.write_bytes(data);p.symlink_to(other)
    elif kind=="fifo":os.mkfifo(p,0o600)
    elif kind=="oversize":p.write_bytes(b" "*(br.MAX_BYTES+1));p.chmod(0o600)
    elif kind=="public":p.write_bytes(data);p.chmod(0o644)
    with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)


@pytest.mark.parametrize("target",["root","reference","ancestor"])
def test_directory_symlinks_refused(fixture,tmp_path,target):
    root,*_,args=fixture
    p=root if target=="root" else root/"reference" if target=="reference" else tmp_path
    moved=p.with_name(p.name+"-moved");p.rename(moved);p.symlink_to(moved,target_is_directory=True)
    try:
        with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)
    finally:p.unlink();moved.rename(p)


def test_public_reference_directory_refused(fixture):
    root,*_,args=fixture
    (root/"reference").chmod(0o755)
    with pytest.raises(br.BindingReadError,match="reference_invalid"):
        br.read_binding_inputs(**args)


def test_foreign_owned_reference_directory_refused(fixture,monkeypatch):
    *_,args=fixture
    original_open=br._Handles.open
    original_stat=os.fstat
    reference_fds=set()
    def track(self,path,**kw):
        fd=original_open(self,path,**kw)
        if path=="reference" and kw.get("directory"):
            reference_fds.add(fd)
        return fd
    def observed_stat(fd):
        info=original_stat(fd)
        if fd in reference_fds:
            fields=list(info)
            fields[4]=os.getuid()+1  # Synthetic observed UID; no host chown.
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(br._Handles,"open",track)
    monkeypatch.setattr(br.os,"fstat",observed_stat)
    with pytest.raises(br.BindingReadError,match="reference_invalid"):
        br.read_binding_inputs(**args)


@pytest.mark.parametrize("target",["root","reference","core","launch"])
def test_replacement_between_read_and_final_check_is_refused(fixture,monkeypatch,target):
    root,_,_,args=fixture;original=sj.boot_id
    def replace():
        p={"root":root,"reference":root/"reference","core":root/sj.CORE_NAME,
           "launch":root/"reference/launch.json"}[target]
        moved=p.with_name(p.name+"-old");p.rename(moved)
        if target in ("root","reference"):p.mkdir(mode=0o700)
        else:p.write_bytes(moved.read_bytes());p.chmod(0o600)
        return original()
    monkeypatch.setattr(sj,"boot_id",replace)
    with pytest.raises(br.BindingReadError):br.read_binding_inputs(**args)


@pytest.mark.parametrize("error",[KeyboardInterrupt,RuntimeError,OverflowError])
def test_unexpected_failure_closes_all_opened_handles(fixture,monkeypatch,error):
    *_,args=fixture;opened=[];original=br._Handles.open
    def record(self,*a,**k):
        fd=original(self,*a,**k);opened.append(fd);return fd
    def fail(*a,**k):raise error("synthetic private failure")
    monkeypatch.setattr(br._Handles,"open",record);monkeypatch.setattr(sj,"boot_id",fail)
    with pytest.raises(error):br.read_binding_inputs(**args)
    assert len(opened)>3
    for fd in opened:
        with pytest.raises(OSError):os.fstat(fd)


def test_secondary_close_failure_keeps_primary_and_attempts_every_close(fixture,monkeypatch):
    *_,args=fixture;original=os.close;attempted=[]
    def fail_primary():raise KeyboardInterrupt("synthetic primary")
    def close(fd):
        attempted.append(fd);original(fd);raise RuntimeError("synthetic secondary")
    monkeypatch.setattr(sj,"boot_id",fail_primary);monkeypatch.setattr(br.os,"close",close)
    with pytest.raises(KeyboardInterrupt):br.read_binding_inputs(**args)
    assert len(attempted)>3
    for fd in attempted:
        with pytest.raises(OSError):os.fstat(fd)


def test_close_failure_cannot_turn_success_into_pass(fixture,monkeypatch):
    *_,args=fixture;original=os.close;attempted=[]
    def close(fd):
        attempted.append(fd);original(fd);raise OSError("synthetic close failure")
    monkeypatch.setattr(br.os,"close",close)
    with pytest.raises(br.BindingReadError,match="binding_read_failed"):
        br.read_binding_inputs(**args)
    assert len(attempted)>5
