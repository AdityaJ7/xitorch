import sys
import inspect
from abc import abstractmethod
from contextlib import contextmanager
import copy
import traceback as tb
import torch

__all__ = ["EditableModule",
    "list_operating_params", "find_param_address", "find_missing_parameters"]

class EditableModule(object):
    @abstractmethod
    def getparams(self, methodname):
        """
        Returns a list of tensor parameters used in the object's operations
        """
        pass

    @abstractmethod
    def setparams(self, methodname, *params):
        """
        Set the input parameters to the object's parameters to make a copy of
        the operations.
        *params is an excessive list of the parameters to be set and the
        method will return the number of parameters it sets.
        """
        pass

    def getuniqueparams(self, methodname):
        allparams = self.getparams(methodname)
        idxs = self._get_unique_params_idxs(methodname, allparams)
        return [allparams[i] for i in idxs]

    def setuniqueparams(self, methodname, *uniqueparams):
        nparams = self._number_of_params[methodname]
        allparams = [None for _ in range(nparams)]
        maps = self._unique_params_maps[methodname]

        for j in range(len(uniqueparams)):
            jmap = maps[j]
            p = uniqueparams[j]
            for i in jmap:
                allparams[i] = p

        return self.setparams(methodname, *allparams)

    def _get_unique_params_idxs(self, methodname, allparams=None):
        if not hasattr(self, "_unique_params_idxs"):
            self._unique_params_idxs = {}
            self._unique_params_maps = {}
            self._number_of_params = {}

        if methodname in self._unique_params_idxs:
            return self._unique_params_idxs[methodname]
        if allparams is None:
            allparams = self.getparams(methodname)

        # get the unique ids
        ids = []
        idxs = []
        idx_map = []
        for i in range(len(allparams)):
            id_param = id(allparams[i])

            # search the id if it has been added to the list
            try:
                jfound = ids.index(id_param)
                idx_map[jfound].append(i)
                continue
            except ValueError:
                pass

            ids.append(id_param)
            idxs.append(i)
            idx_map.append([i])

        self._number_of_params[methodname] = len(allparams)
        self._unique_params_idxs[methodname] = idxs
        self._unique_params_maps[methodname] = idx_map
        return idxs

    @contextmanager
    def useparams(self, methodname, *params):
        try:
            _orig_params_ = self.getuniqueparams(methodname)
            self.setuniqueparams(methodname, *params)
            yield self
        except Exception as exc:
            tb.print_exc()
        finally:
            self.setuniqueparams(methodname, *_orig_params_)

def getmethodparams(method):
    if not inspect.ismethod(method):
        return []
    obj = method.__self__
    methodname = method.__name__
    if not isinstance(obj, EditableModule):
        return []
    return obj.getparams(methodname)

def setmethodparams(method, *params):
    if not inspect.ismethod(method):
        return
    obj = method.__self__
    methodname = method.__name__
    if not isinstance(obj, EditableModule):
        return 0
    return obj.setparams(methodname, *params)

############################ debugging functions ############################

def list_operating_params(method, *args, **kwargs):
    """
    List the tensors used in executing the method
    """
    obj = method.__self__

    # invoke first in case the method add a new variable to the object
    output = method(*args, **kwargs).sum()

    # get all the tensors recursively
    max_depth = 3
    all_tensors, all_names = _get_tensors(obj, prefix="self", max_depth=max_depth)

    # copy the tensors and require them to be differentiable
    copy_tensors0 = [tensor.clone().detach().requires_grad_() for tensor in all_tensors]
    copy_tensors = copy.copy(copy_tensors0)
    _set_tensors(obj, copy_tensors, max_depth=max_depth)

    # run the method and see which one has the gradients
    output = method(*args, **kwargs).sum()
    grad_tensors = torch.autograd.grad(output, copy_tensors0, allow_unused=True)

    # return the original tensor
    all_tensors_copy = copy.copy(all_tensors)
    _set_tensors(obj, all_tensors_copy, max_depth=max_depth)

    names = []
    params = []
    for i, grad in enumerate(grad_tensors):
        if grad is None:
            continue
        names.append(all_names[i])
        params.append(all_tensors[i])

    return names, params

def find_param_address(param, method_or_obj, max_depth=3, return_all=True):
    if inspect.ismethod(method_or_obj):
        obj = method_or_obj.__self__
    else:
        obj = method_or_obj
    all_tensors, all_names = _get_tensors(obj, prefix="self", max_depth=max_depth)
    names = []
    for i,tensor in enumerate(all_tensors):
        if tensor.shape != param.shape: continue
        if not torch.allclose(param, tensor): continue
        names.append(all_names[i])
    if return_all:
        return names
    else:
        return names[0] if len(names) > 0 else None

def find_missing_parameters(method, *args, **kwargs):
    """
    List the parameters missed by the "getparams" function.
    """
    names, params0 = list_operating_params(method, *args, **kwargs)
    obj = method.__self__
    methodname = method.__name__
    params = obj.getuniqueparams(methodname)

    idparams = [id(p) for p in params]
    idparams0 = [id(p) for p in params0]

    missing_names = []
    missing_params = []
    for i in range(len(idparams0)):
        if idparams0[i] not in idparams:
            missing_names.append(names[i])
            missing_params.append(params0[i])

    return missing_names, missing_params

def _get_tensors(obj, prefix, max_depth=4):
    # get the tensors recursively towards torch.nn.Module
    res = []
    names = []
    float_type = [torch.float32, torch.float, torch.float64, torch.float16]
    for key in obj.__dict__:
        elmt = obj.__dict__[key]
        name = "%s.%s"%(prefix, key)
        if isinstance(elmt, torch.Tensor) and elmt.dtype in float_type:
            res.append(elmt)
            names.append(name)
        elif hasattr(elmt, "__dict__"):
            new_res = []
            new_names = []
            if isinstance(elmt, torch.nn.Module):
                new_res, new_names = _get_tensors(elmt, prefix=name, max_depth=max_depth)
            elif max_depth > 0:
                new_res, new_names = _get_tensors(elmt, prefix=name, max_depth=max_depth-1)
            res = res + new_res
            names = names + new_names
        elif hasattr(elmt, "__iter__"):
            for i,elm in enumerate(elmt):
                if not hasattr(elm, "__dict__"): continue
                new_res = []
                new_names = []
                if max_depth > 0:
                    new_res, new_names = _get_tensors(elm, prefix="%s[%d]"%(name,i), max_depth=max_depth-1)
                res = res + new_res
                names = names + new_names
    return res, names

def _set_tensors(obj, all_params, max_depth=4):
    # TODO: set tensors based on the name!
    float_type = [torch.float32, torch.float, torch.float64, torch.float16]
    for key in obj.__dict__:
        elmt = obj.__dict__[key]
        if isinstance(elmt, torch.Tensor) and elmt.dtype in float_type:
            obj.__dict__[key] = all_params.pop(0)
        elif hasattr(elmt, "__dict__"):
            if isinstance(elmt, torch.nn.Module):
                _set_tensors(elmt, all_params, max_depth=max_depth)
            elif max_depth > 0:
                _set_tensors(elmt, all_params, max_depth=max_depth-1)
        elif hasattr(elmt, "__iter__"):
            for i,elm in enumerate(elmt):
                if not hasattr(elm, "__dict__"): continue
                if max_depth > 0:
                    _set_tensors(elm, all_params, max_depth=max_depth-1)
