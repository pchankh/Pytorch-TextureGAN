import torch
from torch.autograd import Variable
from utils import transforms as custom_transforms


# all in one place funcs, need to organize these:
def rand_between(a, b):
    return a + torch.round(torch.rand(1) * (b - a))[0]


def gen_input(img, skg, ini_texture, ini_mask, xcenter=64, ycenter=64, size=40):
    # generate input skg with random patch from img
    # input img,skg [bsx3xwxh], xcenter,ycenter, size
    # output bsx5xwxh

    w, h = img.size()[1:3]
    # print w,h
    xstart = max(int(xcenter - size / 2), 0)
    ystart = max(int(ycenter - size / 2), 0)
    xend = min(int(xcenter + size / 2), w)
    yend = min(int(ycenter + size / 2), h)

    input_texture = ini_texture  # torch.ones(img.size())*(1)
    input_sketch = skg[0:1, :, :]  # L channel from skg
    input_mask = ini_mask  # torch.ones(input_sketch.size())*(-1)

    input_mask[:, xstart:xend, ystart:yend] = 1

    input_texture[:, xstart:xend, ystart:yend] = img[:, xstart:xend, ystart:yend].clone()

    return torch.cat((input_sketch.cpu().float(), input_texture.float(), input_mask), 0)


def gen_input_rand(img, skg, seg, size_min=40, size_max=60, num_patch=1):
    # generate input skg with random patch from img
    # input img,skg [bsx3xwxh], xcenter,ycenter, size
    # output bsx5xwxh
    MAX_COUNT = 10
    bs, c, w, h = img.size()
    results = torch.Tensor(bs, 5, w, h)
    texture_info = []

    # text_info.append([xcenter,ycenter,crop_size])
    seg = seg / torch.max(seg)
    counter = 0
    for i in range(bs):
        counter = 0
        ini_texture = torch.ones(img[0].size()) * (1)
        ini_mask = torch.ones((1, w, h)) * (-1)
        temp_info = []
        for j in range(num_patch):
            crop_size = int(rand_between(size_min, size_max))
            xcenter = int(rand_between(crop_size / 2, w - crop_size / 2))
            ycenter = int(rand_between(crop_size / 2, h - crop_size / 2))
            xstart = max(int(xcenter - crop_size / 2), 0)
            ystart = max(int(ycenter - crop_size / 2), 0)
            xend = min(int(xcenter + crop_size / 2), w)
            yend = min(int(ycenter + crop_size / 2), h)
            patch = seg[i, xstart:xend, ystart:yend]
            sizem = torch.ones(patch.size())
            while torch.sum(patch) >= 0.8 * torch.sum(sizem):
                if counter > MAX_COUNT:
                    break
                crop_size = int(rand_between(size_min, size_max))
                xcenter = int(rand_between(crop_size / 2, w - crop_size / 2))
                ycenter = int(rand_between(crop_size / 2, h - crop_size / 2))

                counter = counter + 1

            temp_info.append([xcenter, ycenter, crop_size])
            res = gen_input(img[i], skg[i], ini_texture, ini_mask, xcenter, ycenter, crop_size)

            ini_texture = res[1:4, :, :]

        texture_info.append(temp_info)
        results[i, :, :, :] = res
    return results, texture_info


def renormalize(img):
    """
    Renormalizes the input image to meet requirements for VGG-19 pretrained network
    """

    forward_norm = torch.ones(img.data.size()) * 0.5
    forward_norm = Variable(forward_norm.cuda())
    img = (img * forward_norm) + forward_norm  # add previous norm
    # return img
    mean = img.data.new(img.data.size())
    std = img.data.new(img.data.size())
    mean[:, 0, :, :] = 0.485
    mean[:, 1, :, :] = 0.456
    mean[:, 2, :, :] = 0.406
    std[:, 0, :, :] = 0.229
    std[:, 1, :, :] = 0.224
    std[:, 2, :, :] = 0.225
    img -= Variable(mean)
    img = img / Variable(std)

    return img


def train(model, train_loader, val_loader, input_stack, target_img, target_texture,
          segment, label, extract_content, extract_style, loss_graph, vis, args):

    netG = model["netG"]
    netD = model["netD"]
    criterion_gan = model["criterion_gan"]
    criterion_pixel_l = model["criterion_pixel_l"]
    criterion_pixel_ab = model["criterion_pixel_ab"]
    criterion_feat = model["criterion_feat"]
    criterion_style = model["criterion_style"]
    real_label = model["real_label"]
    fake_label = model["fake_label"]
    optimizerD = model["optimizerD"]
    optimizerG = model["optimizerG"]

    for i, data in enumerate(train_loader):

        # Detach is apparently just creating new Variable with cut off reference to previous node, so shouldn't effect the original
        # But just in case, let's do G first so that detaching G during D update don't do anything weird
        ############################
        # (1) Update G network: maximize log(D(G(z)))
        ###########################
        netG.zero_grad()

        img, skg, seg, txt = data  # LAB with negeative value
        # output img/skg/seg rgb between 0-1
        # output img/skg/seg lab between 0-100, -128-128
        if args.color_space == 'lab':
            img = custom_transforms.normalize_lab(img)
            skg = custom_transforms.normalize_lab(skg)
            txt = custom_transforms.normalize_lab(txt)
            seg = custom_transforms.normalize_seg(seg)
            # seg = custom_transforms.normalize_lab(seg)
        elif args.color_space == 'rgb':
            img = custom_transforms.normalize_rgb(img)
            skg = custom_transforms.normalize_rgb(skg)
            txt = custom_transforms.normalize_rgb(txt)
            # seg=custom_transforms.normalize_rgb(seg)
        # print seg

        if not args.use_segmentation_patch:
            seg.fill_(1)

        bs, w, h = seg.size()

        seg = seg.view(bs, 1, w, h)
        seg = torch.cat((seg, seg, seg), 1)
        # import pdb; pdb.set_trace()

        temp = torch.ones(seg.size()) * (1 - seg).float()
        temp[:, 1, :, :] = 0  # torch.ones(seg[:,1,:,:].size())*(1-seg[:,1,:,:]).float()
        temp[:, 2, :, :] = 0  # torch.ones(seg[:,2,:,:].size())*(1-seg[:,2,:,:]).float()

        txt = txt.float() * seg.float() + temp

        if args.input_texture_patch == 'original_image':
            inp, _ = gen_input_rand(img, skg, seg[:, 0, :, :], args.patch_size_min, args.patch_size_max,
                                    args.num_input_texture_patch)
        elif args.input_texture_patch == 'dtd_texture':
            inp, _ = gen_input_rand(txt, skg, seg[:, 0, :, :], args.patch_size_min, args.patch_size_max,
                                    args.num_input_texture_patch)

        batch_size, _, _, _ = img.size()

        img = img.cuda()
        skg = skg.cuda()
        seg = seg.cuda()
        txt = txt.cuda()

        inp = inp.cuda()

        input_stack.resize_as_(inp.float()).copy_(inp)
        target_img.resize_as_(img.float()).copy_(img)
        segment.resize_as_(seg.float()).copy_(seg)
        target_texture.resize_as_(txt.float()).copy_(txt)

        assert torch.max(seg) <= 1

        inputv = Variable(input_stack)
        gtimgv = Variable(target_img)
        segv = Variable(segment)
        txtv = Variable(target_texture)

        outputG = netG(inputv)

        outputl, outputa, outputb = torch.chunk(outputG, 3, dim=1)

        gtl, gta, gtb = torch.chunk(gtimgv, 3, dim=1)
        txtl, txta, txtb = torch.chunk(txtv, 3, dim=1)

        outputab = torch.cat((outputa, outputb), 1)
        gtab = torch.cat((gta, gtb), 1)
        txtab = torch.cat((txta, txtb), 1)

        if args.color_space == 'lab':
            outputlll = (torch.cat((outputl, outputl, outputl), 1))
            gtlll = (torch.cat((gtl, gtl, gtl), 1))
            txtlll = torch.cat((txtl, txtl, txtl), 1)
        elif args.color_space == 'rgb':
            outputlll = outputG  # (torch.cat((outputl,outputl,outputl),1))
            gtlll = gtv  # (torch.cat((targetl,targetl,targetl),1))
            txtlll = txtv
        if args.loss_texture == 'original_image':
            targetl = gtl
            targetab = gtab
            targetlll = gtlll
        else:
            # if args.loss_texture == 'texture_mask':
            # remove baskground dtd
            #     txtl = segv[:,0:1,:,:]*txtl
            #     txtab=segv[:,1:3,:,:]*txtab
            #     txtlll=segv*txtlll
            # elif args.loss_texture == 'texture_patch':

            targetl = txtl
            targetab = txtab
            targetlll = txtlll

            # print seg

        # import pdb; pdb.set_trace()

        ##################Pixel L Loss############################
        err_pixel_l = args.pixel_weight_l * criterion_pixel_l(outputl, targetl)

        ##################Pixel ab Loss############################
        err_pixel_ab = args.pixel_weight_ab * criterion_pixel_ab(outputab, targetab)

        ##################feature Loss############################
        out_feat = extract_content(renormalize(outputlll))[0]

        gt_feat = extract_content(renormalize(gtlll))[0]
        err_feat = args.feature_weight * criterion_feat(out_feat, gt_feat.detach())

        ##################style Loss############################


        if args.local_texture_size == -1:  # global
            output_feat_ = extract_style(outputlll)
            target_feat_ = extract_style(targetlll)
        else:
            patchsize = args.local_texture_size
            x = int(rand_between(patchsize, args.image_size - patchsize))
            y = int(rand_between(patchsize, args.image_size - patchsize))

            texture_patch = outputlll[:, :, x:(x + patchsize), y:(y + patchsize)]
            gt_texture_patch = targetlll[:, :, x:(x + patchsize), y:(y + patchsize)]
            output_feat_ = extract_style(texture_patch)
            target_feat_ = extract_style(gt_texture_patch)

        gram = GramMatrix()

        err_style = 0
        for m in range(len(output_feat_)):
            gram_y = gram(output_feat_[m])
            gram_s = gram(target_feat_[m])

            err_style += args.style_weight * criterion_style(gram_y, gram_s.detach())

        ################## D Loss ############################
        netD.zero_grad()
        label_ = Variable(label)
        if args.color_space == 'lab':
            outputD = netD(outputl)
        elif args.color_space == 'rgb':
            outputD = netD(outputG)
        # D_G_z2 = outputD.data.mean()

        label.resize_(outputD.data.size())
        labelv = Variable(label.fill_(real_label))

        err_gan = args.discriminator_weight * criterion_gan(outputD, labelv)

        ####################################
        err_G = err_pixel_l + err_pixel_ab + err_gan + err_feat + err_style
        err_G.backward()

        optimizerG.step()

        loss_graph["g"].append(err_G.data[0])
        loss_graph["gpl"].append(err_pixel_l.data[0])
        loss_graph["gpab"].append(err_pixel_ab.data[0])
        loss_graph["gd"].append(err_gan.data[0])
        loss_graph["gf"].append(err_feat.data[0])
        loss_graph["gs"].append(err_style.data[0])

        print('G:', i, err_G.data[0])

        ############################
        # (2) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
        ###########################
        # train with real

        netD.zero_grad()

        labelv = Variable(label)
        if args.color_space == 'lab':
            outputD = netD(targetl)
        elif args.color_space == 'rgb':
            outputD = netD(gtv)

        label.resize_(outputD.data.size())
        labelv = Variable(label.fill_(real_label))

        errD_real = criterion_gan(outputD, labelv)
        errD_real.backward()

        score = Variable(torch.ones(batch_size))
        _, cd, wd, hd = outputD.size()
        D_output_size = cd * wd * hd

        clamped_output_D = outputD.clamp(0, 1)
        clamped_output_D = torch.round(clamped_output_D)
        for acc_i in range(batch_size):
            score[acc_i] = torch.sum(clamped_output_D[acc_i]) / D_output_size

        real_acc = torch.mean(score)

        if args.color_space == 'lab':
            outputD = netD(outputl.detach())
        elif args.color_space == 'rgb':
            outputD = netD(outputG.detach())
        label.resize_(outputD.data.size())
        labelv = Variable(label.fill_(fake_label))

        errD_fake = criterion_gan(outputD, labelv)
        errD_fake.backward()
        score = Variable(torch.ones(batch_size))
        _, cd, wd, hd = outputD.size()
        D_output_size = cd * wd * hd

        clamped_output_D = outputD.clamp(0, 1)
        clamped_output_D = torch.round(clamped_output_D)
        for acc_i in range(batch_size):
            score[acc_i] = torch.sum(clamped_output_D[acc_i]) / D_output_size

        fake_acc = torch.mean(1 - score)

        D_acc = (real_acc + fake_acc) / 2

        if D_acc.data[0] < args.threshold_D_max:
            # D_G_z1 = output.data.mean()
            errD = errD_real + errD_fake
            loss_graph["d"].append(errD.data[0])
            optimizerD.step()
        else:
            loss_graph["d"].append(0)

        print('D:', 'real_acc', "%.2f" % real_acc.data[0], 'fake_acc', "%.2f" % fake_acc.data[0], 'D_acc',
              D_acc.data[0])
        if i % args.save_every == 0:
            save_network(netG, 'G', i, args.gpu, args.save_dir)
            save_network(netD, 'D', i, args.gpu, args.save_dir)

        if i % args.visualize_every == 0:
            imgs = []
            for ii, data in enumerate(val_loader, 0):
                img, skg, seg, txt = data  # LAB with negeative value
                # this is in LAB value 0/100, -128/128 etc
                img = custom_transforms.normalize_lab(img)
                skg = custom_transforms.normalize_lab(skg)
                txt = custom_transforms.normalize_lab(txt)
                seg = custom_transforms.normalize_seg(seg)

                bs, w, h = seg.size()

                seg = seg.view(bs, 1, w, h)
                seg = torch.cat((seg, seg, seg), 1)
                # import pdb; pdb.set_trace()

                temp = torch.ones(seg.size()) * (1 - seg).float()
                temp[:, 1, :, :] = 0  # torch.ones(seg[:,1,:,:].size())*(1-seg[:,1,:,:]).float()
                temp[:, 2, :, :] = 0  # torch.ones(seg[:,2,:,:].size())*(1-seg[:,2,:,:]).float()

                txt = txt.float() * seg.float() + temp
                # seg=custom_transforms.normalize_lab(seg)
                # norm to 0-1 minus mean
                if not args.use_segmentation_patch:
                    seg.fill_(1)
                if args.input_texture_patch == 'original_image':
                    inp, texture_loc = gen_input_rand(img, skg, seg[:, 0, :, :] * 100,
                                                      args.patch_size_min, args.patch_size_max,
                                                      args.num_input_texture_patch)
                elif args.input_texture_patch == 'dtd_texture':
                    inp, texture_loc = gen_input_rand(txt, skg, seg[:, 0, :, :] * 100,
                                                      args.patch_size_min, args.patch_size_max,
                                                      args.num_input_texture_patch)

                img = img.cuda()
                skg = skg.cuda()
                seg = seg.cuda()
                txt = txt.cuda()
                inp = inp.cuda()
                print
                inp.size()

                input_stack.resize_as_(inp.float()).copy_(inp)
                target_img.resize_as_(img.float()).copy_(img)
                segment.resize_as_(seg.float()).copy_(seg)

                inputv = Variable(input_stack)
                targetv = Variable(target_img)

                outputG = netG(inputv)

                # segment_img=vis_image((seg.cpu()))
                # segment_img=(segment_img*255).astype('uint8')
                # segment_img=np.transpose(segment_img,(2,0,1))
                # imgs.append(segment_img)

                # inp_img= vis_patch(custom_transforms.denormalize_lab(img.cpu()), custom_transforms.denormalize_lab(skg.cpu()), xcenter, ycenter, crop_size)
                # inp_img=(inp_img*255).astype('uint8')
                # inp_img=np.transpose(inp_img,(2,0,1))
                # imgs.append(inp_img)

                # out_img= vis_image(custom_transforms.denormalize_lab(outputG.data.double().cpu()))
                # out_img=(out_img*255).astype('uint8')
                # out_img=np.transpose(out_img,(2,0,1))
                # imgs.append(out_img)

                # tar_img=vis_image(custom_transforms.denormalize_lab(img.cpu()))
                # tar_img=(tar_img*255).astype('uint8')
                # tar_img=np.transpose(tar_img,(2,0,1))
                # imgs.append(tar_img)

            if args.color_space == 'lab':
                out_img = vis_image(custom_transforms.denormalize_lab(outputG.data.double().cpu()),
                                    args.color_space)
                if args.input_texture_patch == 'original_image':
                    inp_img = vis_patch(custom_transforms.denormalize_lab(img.cpu()),
                                        custom_transforms.denormalize_lab(skg.cpu()),
                                        texture_loc,
                                        args.color_space)
                elif args.input_texture_patch == 'dtd_texture':
                    inp_img = vis_patch(custom_transforms.denormalize_lab(txt.cpu()),
                                        custom_transforms.denormalize_lab(skg.cpu()),
                                        texture_loc,
                                        args.color_space)
                tar_img = vis_image(custom_transforms.denormalize_lab(img.cpu()),
                                    args.color_space)
            elif args.color_space == 'rgb':

                out_img = vis_image(custom_transforms.denormalize_rgb(outputG.data.double().cpu()),
                                    args.color_space)
                inp_img = vis_patch(custom_transforms.denormalize_rgb(img.cpu()),
                                    custom_transforms.denormalize_rgb(skg.cpu()),
                                    texture_loc,
                                    args.color_space)
                tar_img = vis_image(custom_transforms.denormalize_rgb(img.cpu()),
                                    args.color_space)

            out_img = [x * 255 for x in out_img]  # (out_img*255)#.astype('uint8')
            # out_img=np.transpose(out_img,(2,0,1))

            inp_img = [x * 255 for x in inp_img]  # (inp_img*255)#.astype('uint8')
            # inp_img=np.transpose(inp_img,(2,0,1))


            tar_img = [x * 255 for x in tar_img]  # (tar_img*255)#.astype('uint8')
            # tar_img=np.transpose(tar_img,(2,0,1))

            segment_img = vis_image((seg.cpu()), args.color_space)
            segment_img = [x * 255 for x in segment_img]  # segment_img=(segment_img*255)#.astype('uint8')
            # segment_img=np.transpose(segment_img,(2,0,1))

            for i_ in range(len(out_img)):
                imgs.append(segment_img[i_])
                imgs.append(inp_img[i_])
                imgs.append(out_img[i_])
                imgs.append(tar_img[i_])

            # for idx, img in enumerate(imgs):
            #     print(idx, type(img), img.shape)

            vis.images(imgs, win='output', opts=dict(title='Output images'))
            # vis.image(inp_img,win='input',opts=dict(title='input'))
            # vis.image(tar_img,win='target',opts=dict(title='target'))
            # vis.image(segment_img,win='segment',opts=dict(title='segment'))
            vis.line(np.array(loss_graph["gs"]), win='gs', opts=dict(title='G-Style Loss'))
            vis.line(np.array(loss_graph["g"]), win='g', opts=dict(title='G Total Loss'))
            vis.line(np.array(loss_graph["gd"]), win='gd', opts=dict(title='G-Discriminator Loss'))
            vis.line(np.array(loss_graph["gf"]), win='gf', opts=dict(title='G-Feature Loss'))
            vis.line(np.array(loss_graph["gpl"]), win='gpl', opts=dict(title='G-Pixel Loss-L'))
            vis.line(np.array(loss_graph["gpab"]), win='gpab', opts=dict(title='G-Pixel Loss-AB'))
            vis.line(np.array(loss_graph["d"]), win='d', opts=dict(title='D Loss'))